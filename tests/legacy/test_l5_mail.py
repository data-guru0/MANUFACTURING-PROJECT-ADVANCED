"""L5 Done-when: `rfq_traffic --burst 20` delivers 20 emails, which are read back over IMAP with their
attachments and (cryptographically verified) DKIM signatures. Also: TLS + login are enforced, both
responders answer over real email, W18 evidence exists, and the labelled mix has the promised variety.
Runs against the live mail server, the responder containers and Odoo."""

import email
import imaplib
import json
import re
import smtplib
import subprocess
import sys
import time
import uuid
from collections import Counter
from email.message import EmailMessage, Message
from email.utils import formatdate
from pathlib import Path
from typing import Any

import dkim
import pymupdf
import pytest

from legacy.mail.client import imap, send, text_of, tls
from legacy.odoo.rpc import Odoo, env
from legacy.traffic.customer_responder import answer, outcome_for
from legacy.traffic.rfq_mix import build_mix
from legacy.traffic.rfq_traffic import LABELS, sent_count

REPO = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(REPO / "docker-compose.legacy.yml")]


def find(m: imaplib.IMAP4_SSL, header: str, value: str, timeout: float = 90) -> bytes:
    """Raw bytes of the newest INBOX message whose `header` equals `value`; waits for delivery."""
    end = time.time() + timeout
    while True:
        m.select("INBOX", readonly=True)
        _, data = m.uid("search", "HEADER", header, value)
        if data[0]:
            _, got = m.uid("fetch", data[0].split()[-1].decode(), "(BODY.PEEK[])")
            raw: bytes = next(p[1] for p in got if isinstance(p, tuple))
            return raw
        if time.time() > end:
            raise AssertionError(f"no message with {header}: {value}")
        time.sleep(2)


def dkim_record(domain: str) -> bytes:
    """The public key as it would be published in DNS, read from the mail server's own key folder."""
    out = subprocess.run([*COMPOSE, "exec", "-T", "mail", "cat", f"/tmp/docker-mailserver/opendkim/keys/{domain}/mail.txt"],
                         check=True, capture_output=True, text=True).stdout  # fmt: skip
    return "".join(re.findall(r'"([^"]*)"', out)).encode()


def attachment_names(msg: Message) -> list[str]:
    return sorted(str(p.get_filename()) for p in msg.walk() if p.get_filename())


def test_burst_20_is_delivered_with_attachments_and_valid_dkim() -> None:
    before = sent_count()
    r = subprocess.run([sys.executable, "-m", "legacy.traffic.rfq_traffic", "--burst", "20"], cwd=REPO,
                       capture_output=True, text=True, encoding="utf-8", timeout=300)  # fmt: skip
    assert r.returncode == 0, r.stderr
    labels = [json.loads(x) for x in LABELS.read_text(encoding="utf-8").splitlines()[before:]]
    assert len(labels) == 20
    keys = {d: dkim_record(d) for d in ("customers.local", "suppliers.local")}

    def dns(name: bytes, timeout: int = 5) -> bytes:
        return keys[name.decode().rstrip(".").removeprefix("mail._domainkey.")]

    m = imap("rfq@apex.local")
    try:
        for lab in labels:
            raw = find(m, "Message-ID", lab["message_id"])
            msg = email.message_from_bytes(raw)
            assert attachment_names(msg) == sorted(a["filename"] for a in lab["attachments"]), lab["subject"]
            sig = str(msg["DKIM-Signature"])
            assert f"d={lab['from'].rsplit('@', 1)[1]}" in sig.replace(" ", "")
            assert dkim.verify(raw, dnsfunc=dns), f"DKIM signature does not verify: {lab['subject']}"
    finally:
        m.logout()


def test_drip_sends_with_random_gaps() -> None:
    before = sent_count()
    r = subprocess.run([sys.executable, "-m", "legacy.traffic.rfq_traffic", "--drip", "--per-hour", "3600", "--count", "2"],
                       cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=120)  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert sent_count() == before + 2


def test_wrong_password_is_refused() -> None:
    m = imaplib.IMAP4_SSL("127.0.0.1", 993, ssl_context=tls())
    with pytest.raises(imaplib.IMAP4.error):
        m.login("rfq@apex.local", "not-the-password")


def test_no_login_without_tls() -> None:
    with smtplib.SMTP("127.0.0.1", 587, timeout=30) as s:
        s.ehlo()
        assert not s.has_extn("auth"), "the server must not offer AUTH before STARTTLS"
        with pytest.raises(smtplib.SMTPNotSupportedError):
            s.login("rfq@apex.local", env("MAIL_RFQ_PASSWORD"))


def _quote_mail(sender: str, to: str, subject: str, body: str, mid: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Date"], msg["Message-ID"] = sender, to, subject, formatdate(), mid
    msg.set_content(body)
    return msg


def test_customer_responder_answers_a_quote_by_email() -> None:
    i = 0
    while outcome_for(mid := f"<l5test-{uuid.uuid4().hex}-{i}@apex.local>") == "silence":
        i += 1  # pick an email the customer will answer
    send("estimator1@apex.local", _quote_mail("estimator1@apex.local", "kestrelautomation@customers.local",
         "Quotation S09999 for D-20001-A", "Dear customer, our price is 41.20 USD per piece.", mid))  # fmt: skip
    m = imap("estimator1@apex.local")
    try:
        got = email.message_from_bytes(find(m, "In-Reply-To", mid))
    finally:
        m.logout()
    assert answer(outcome_for(mid), mid).splitlines()[0] in text_of(got)
    assert "customers.local" in str(got["From"]) and str(got["Subject"]).startswith("Re:")


def _supplier_price(msg: Message) -> float:
    texts = [text_of(msg)] + [pymupdf.open(stream=p.get_payload(decode=True), filetype="pdf")[0].get_text()
                              for p in msg.walk() if str(p.get_filename() or "").endswith(".pdf")]  # fmt: skip
    found = re.search(r"\(MAT-AL6061\): ([\d.]+) USD per kg", "\n".join(texts))
    assert found, texts
    return float(found.group(1))


def test_supplier_responder_quotes_from_its_odoo_pricelist() -> None:
    mid = f"<l5test-{uuid.uuid4().hex}@apex.local>"
    send("purchasing@apex.local", _quote_mail("purchasing@apex.local", "midwestmetalssupply@suppliers.local",
         "Price request Al 6061", "Hi, please quote Al 6061-T6 plate 25 mm, 40 kg. Lead time?\nTom", mid))  # fmt: skip
    m = imap("purchasing@apex.local")
    try:
        got = email.message_from_bytes(find(m, "In-Reply-To", mid))
    finally:
        m.logout()
    o = Odoo("office", env("ODOO_OFFICE_PASSWORD"))
    domain = [("partner_id.ref", "=", "SUP-01"), ("product_tmpl_id.default_code", "=", "MAT-AL6061")]
    rows: list[dict[str, Any]] = o.call("product.supplierinfo", "search_read", domain, fields=["price"], order="id")
    assert rows[-1]["price"] * 0.97 - 0.01 <= _supplier_price(got) <= rows[-1]["price"] * 1.12 + 0.01


def test_w18_purchasing_forwarded_whole_confidential_drawings() -> None:
    m = imap("precisionpartnersmac@suppliers.local")
    try:
        m.select("INBOX", readonly=True)
        _, data = m.uid("search", "FROM", "purchasing@apex.local", "SUBJECT", '"FW: RFQ"')
        assert data[0], "no forwarded customer drawing at the subcontractor"
        _, got = m.uid("fetch", data[0].split()[0].decode(), "(BODY.PEEK[])")
        msg = email.message_from_bytes(next(p[1] for p in got if isinstance(p, tuple)))
    finally:
        m.logout()
    pdf = next(p for p in msg.walk() if str(p.get_filename() or "").endswith(".pdf"))
    text = pymupdf.open(stream=pdf.get_payload(decode=True), filetype="pdf")[0].get_text()
    owner = re.search(r"PROPERTY OF (.+?) - CONFIDENTIAL", text)
    assert owner, "the forwarded drawing should still carry the customer's title block"
    o = Odoo("office", env("ODOO_OFFICE_PASSWORD"))
    nda = o.call("res.partner", "search_read", [("x_nda_restricted", "=", True)], fields=["name"])
    assert owner.group(1).strip() in {p["name"].upper() for p in nda}, "an NDA customer's drawing went to a supplier"


def test_the_labelled_mix_has_the_promised_variety() -> None:
    mix = build_mix()
    labels = [x.label for x in mix]
    items = [it for lab in labels for it in lab["items"]]
    assert sorted(it["rfq_id"] for it in items) == [f"RFQ-{i:03d}" for i in range(1, 81)]  # every part exactly once
    assert {lab["language"] for lab in labels} == {"en", "de", "es", "hi"}
    assert any(len(lab["items"]) > 1 for lab in labels) and any(lab["category"] == "rfq" and not lab["attachments"]
                                                                for lab in labels)  # fmt: skip
    types = Counter(a["true_type"] for lab in labels for a in lab["attachments"])
    assert {"pdf", "pdf_scan", "dxf", "step", "zip"} <= set(types)
    kinds = Counter(it["kind"] for it in items)
    assert kinds["repeat"] == 10 and kinds["revision"] == 12 and kinds["out_of_capability"] == 8
    reqs = [lab["requests"] for lab in labels if lab["category"] == "rfq"]
    for key in ("co2", "fai", "ppap", "export_control", "incoterms", "requested_date", "certs"):
        assert any(r[key] for r in reqs), key
    assert {lab["intent"] for lab in labels if lab["category"] == "non_rfq"} >= {"invoice", "newsletter", "cv"}
    assert len({lab["attack"] for lab in labels if lab["attack"]}) >= 6
