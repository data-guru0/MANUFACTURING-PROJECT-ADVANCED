"""S8 Done-when (the pilot demo): a customer emails an RFQ; a costed draft with a delivery date appears in the review
screen; an estimator approves it (real Keycloak login in Edge); the customer's mailbox receives the threaded quote PDF;
the draft quotation is in Odoo. Plus: the write tools refuse anything but the approved content (real token, tampered
content), repeating act changes nothing, the outcome comes back from Odoo, a model is promoted only by a person and only
when it is better, and the approval key can't be read from OPA."""

import email
import hashlib
import imaplib
import json
import subprocess
from email import policy
from email.message import EmailMessage
from typing import Any

import pymupdf
import pytest
from playwright.sync_api import Browser, expect

from legacy.mail.client import password, tls
from legacy.odoo.rpc import Odoo, env
from tests.agent.test_s7_review import COMPOSE, REPO, UI, browser, login, rfq, sql

__all__ = ["browser"]  # the Edge fixture, shared with S7

CUSTOMER = "kestrelautomation@customers.local"


def compose(*args: str, stdin: str | None = None, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*COMPOSE, *args], input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=timeout)


@pytest.fixture(scope="module")
def sent(browser: Browser) -> dict[str, Any]:
    """The pilot: a fresh RFQ with two quantities -> review -> arjun approves in the screen -> the worker acts."""
    e = rfq(CUSTOMER, "Kestrel Automation", "25 pcs and 100 pcs")
    page = login(browser, "arjun")
    page.goto(f"{UI}/jobs/{e}")
    expect(page.locator("h1")).to_contain_text(f"#{e}")
    page.fill("#reason", "pilot: drawing, price and date checked")
    page.get_by_role("button", name="Approve", exact=True).click()
    expect(page.locator("#result")).to_contain_text("the job is sent", timeout=300_000)
    page.goto(f"{UI}/jobs/{e}")
    expect(page.locator("#acted")).to_contain_text("Odoo draft quotation")
    rfq_id = sql(f"SELECT message_id FROM emails WHERE id = {e}")[0][0]
    result = json.loads(sql(f"SELECT result FROM actions WHERE email_id = {e} ORDER BY id DESC LIMIT 1")[0][0])
    return {"email_id": e, "rfq_message_id": rfq_id, **result}


def customer_reply(rfq_message_id: str) -> EmailMessage:
    m = imaplib.IMAP4_SSL("127.0.0.1", 993, ssl_context=tls())
    try:
        m.login(CUSTOMER, password(CUSTOMER))
        m.select("INBOX")
        _, found = m.search(None, "HEADER", "In-Reply-To", rfq_message_id)
        ids = found[0].split()
        assert len(ids) == 1, f"expected exactly one reply in the customer's inbox, found {len(ids)}"
        _, data = m.fetch(ids[0], "(RFC822)")
    finally:
        m.logout()
    raw = data[0]
    assert isinstance(raw, tuple)
    msg = email.message_from_bytes(raw[1], policy=policy.default)
    assert isinstance(msg, EmailMessage)
    return msg


def test_the_customer_receives_the_threaded_quote_pdf(sent: dict[str, Any]) -> None:
    msg = customer_reply(sent["rfq_message_id"])
    assert msg["From"].addresses[0].addr_spec == "rfq@apex.local" and msg["Message-ID"] == sent["message_id"]
    assert sent["rfq_message_id"] in msg["References"] and msg["DKIM-Signature"]  # threaded, signed by apex.local
    body = msg.get_body(("plain",))
    assert body is not None
    approved_md5 = sql(f"SELECT md5(btrim(final_text, E' \\n')) FROM decisions WHERE email_id = {sent['email_id']}")[0][0]
    received = body.get_content().replace("\r\n", "\n").strip(" \n")  # mail travels with CRLF line ends
    assert hashlib.md5(received.encode()).hexdigest() == approved_md5  # exactly the approved text
    (pdf,) = [a for a in msg.iter_attachments() if a.get_content_type() == "application/pdf"]
    assert pdf.get_filename() == f"Apex quotation {sent['quotation']}.pdf"
    text = pymupdf.open(stream=pdf.get_content(), filetype="pdf")[0].get_text()
    est = json.loads(sql(f"SELECT result FROM estimates WHERE email_id = {sent['email_id']}")[0][0])
    for b in est["breaks"]:  # every price break, exactly as computed (the PDF is built from the approved estimates)
        assert f"{b['unit_price']:,.2f}" in text, (b, text[:500])
    assert est["delivery"]["committed"] in text and "Approved by Arjun" in text and sent["quotation"] in text


def test_the_draft_quotation_is_in_odoo(sent: dict[str, Any]) -> None:
    o = Odoo("admin", env("ODOO_ADMIN_PASSWORD"))
    (so,) = o.call("sale.order", "search_read", [("client_order_ref", "=", f"AGENT-RFQ-{sent['email_id']}")],
                   fields=["name", "state", "validity_date", "x_quote_sent_at", "order_line", "sale_order_option_ids", "create_uid",
                           "partner_id", "x_rfq_received_at"])  # fmt: skip
    est = json.loads(sql(f"SELECT result FROM estimates WHERE email_id = {sent['email_id']}")[0][0])
    assert so["name"] == sent["quotation"] and so["state"] == "draft"  # a draft: Apex confirms, the agent never does
    assert so["create_uid"][1] == "Apex agent (integration)" and "Kestrel" in so["partner_id"][1]
    assert so["validity_date"] == est["terms"]["validity_date"] and so["x_quote_sent_at"] and so["x_rfq_received_at"]
    (line,) = o.call("sale.order.line", "read", so["order_line"], ["product_uom_qty", "price_unit"])
    assert (line["product_uom_qty"], line["price_unit"]) == (est["breaks"][0]["qty"], round(est["breaks"][0]["unit_price"], 2))
    options = o.call("sale.order.option", "read", so["sale_order_option_ids"], ["quantity", "price_unit"])
    assert [(x["quantity"], x["price_unit"]) for x in options] == [
        (b["qty"], round(b["unit_price"], 2)) for b in est["breaks"][1:]
    ]
    files = {
        a["name"]
        for a in o.call(
            "ir.attachment", "search_read", [("res_model", "=", "sale.order"), ("res_id", "=", so["id"])], fields=["name"]
        )
    }
    assert f"Apex quotation {so['name']}.pdf" in files and any(f.endswith(".dxf") for f in files), files


def test_the_write_tools_refuse_anything_but_the_approved_content(sent: dict[str, Any]) -> None:
    script = (REPO / "tests" / "agent" / "s8_replay.py").read_text(encoding="utf-8")
    r = compose("exec", "-T", "worker", "python", "-", str(sent["email_id"]), stdin=script)
    assert r.returncode == 0, r.stderr[-1500:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    # the same approved content again: nothing new (idempotent)
    assert out["quotation_again"]["ok"] and not out["quotation_again"]["result"]["created"], out["quotation_again"]
    assert out["reply_again"]["ok"] and out["reply_again"]["result"]["already_sent"], out["reply_again"]
    # with the person's REAL token but other content: refused by OPA inside the tool
    assert "prices differ from the ones the person approved" in out["quotation_cheaper"]["result"]
    assert "email differs from the one the person approved" in out["reply_changed"]["result"]
    assert "email differs from the one the person approved" in out["reply_elsewhere"]["result"]
    assert out["reply_for_another_job"] is False
    customer_reply(sent["rfq_message_id"])  # still exactly one reply in the customer's inbox


def test_learn_reads_the_outcome_back_from_odoo(sent: dict[str, Any]) -> None:
    """Apex cancels the quotation in Odoo (the customer said no); the worker's learn sync records it as lost."""
    o = Odoo("admin", env("ODOO_ADMIN_PASSWORD"))
    (so,) = o.call("sale.order", "search", [("client_order_ref", "=", f"AGENT-RFQ-{sent['email_id']}")])
    o.call("sale.order", "write", [so], {"x_lost_reason": "Price too high (S8 test)"})
    o.call("sale.order", "action_cancel", [so])
    r = compose("exec", "-T", "worker", "python", "-m", "apex_agent.actions.outcomes")
    assert r.returncode == 0, r.stderr[-1000:]
    assert sql(f"SELECT outcome, state, lost_reason FROM outcomes WHERE email_id = {sent['email_id']}")[0] == [
        "lost", "cancel", "Price too high (S8 test)"]  # fmt: skip


def test_a_cli_decision_is_recorded_but_never_sends() -> None:
    """The operator's CLI has no signed approval: act records the decision and sends nothing."""
    rows = sql("""SELECT d.email_id FROM decisions d JOIN jobs j USING (email_id)
                  WHERE d.decided_by = 'Ramesh' AND d.action IN ('approve', 'edit') AND j.status = 'done'""")  # fmt: skip
    assert rows, "test_s6_graph.py approves and edits through the CLI first"
    ids = ",".join(r[0] for r in rows)
    assert sql(f"SELECT count(*) FROM actions WHERE email_id IN ({ids})")[0][0] == "0"


def test_a_model_is_promoted_only_by_a_person_and_only_when_better() -> None:
    def latest() -> str:
        r = compose("run", "--rm", "knowledge", "python", "-c",
                    "import json; from apex_agent.ingest.store import Store; print(json.loads(Store().get('models/cycle_time/latest.json'))['version'])")  # fmt: skip
        assert r.returncode == 0, r.stderr[-800:]
        return r.stdout.strip().splitlines()[-1]

    before = latest()
    r = compose("run", "--rm", "knowledge", "python", "-m", "apex_agent.costing.cycle_time", "train")
    assert r.returncode == 0, r.stderr[-1500:]
    card = json.loads(r.stdout.strip().splitlines()[-1])
    assert card["status"].startswith("candidate") and latest() == before  # training alone changes nothing
    nobody = compose(
        "run", "--rm", "knowledge", "python", "-m", "apex_agent.costing.cycle_time", "promote", card["version"], "--by", " "
    )
    assert nobody.returncode != 0 and "a person promotes a model" in nobody.stderr
    r = compose(
        "run", "--rm", "knowledge", "python", "-m", "apex_agent.costing.cycle_time", "promote", card["version"], "--by", "Ramesh"
    )
    if r.returncode == 0:  # it really was better on the held-out work orders
        rec = json.loads(r.stdout.strip().splitlines()[-1])
        assert (
            rec["promoted_by"] == "Ramesh"
            and rec["held_out"]["candidate"]["mdape_p50"] < rec["held_out"]["previous"]["mdape_p50"]
        )
        assert latest() == card["version"]
    else:  # same data, same model: not better, so refused, and the current model stays
        assert "is not better than current" in r.stderr, r.stderr[-800:]
        assert latest() == before


def test_the_approval_key_cannot_be_read_from_opa() -> None:
    """The worker can ask OPA anything, but the signing key is only used inside a function, never a readable value."""
    r = compose("exec", "-T", "worker", "python", "-c",
                "import httpx; print(httpx.post('http://opa:8181/v1/data', json={'input': {}}).text)")  # fmt: skip
    assert r.returncode == 0, r.stderr[-500:]
    leaked = env("APPROVAL_SIGNING_KEY") in r.stdout
    assert not leaked, "the approval signing key is readable through OPA's data API"
