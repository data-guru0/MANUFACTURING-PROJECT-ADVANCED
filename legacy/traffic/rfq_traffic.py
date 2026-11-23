"""Send the labelled RFQ mix (rfq_mix.py) to rfq@apex.local as real email, over authenticated SMTP.

  --burst N            send the next N emails of the mix now
  --drip [--per-hour R] [--count N]
                       Poisson arrivals: random gaps averaging 3600/R seconds (Ctrl+C to stop)
  --purchasing-history W18 evidence: purchasing@ forwards whole customer drawings (title block:
                       "PROPERTY OF <CUSTOMER> - CONFIDENTIAL") to suppliers. Idempotent.

The position in the mix is the number of lines in data/generated/rfq_labels.jsonl; each sent email
appends its label (with its Message-ID) there. After the 68th email the mix starts again (cycle 2).
"""

import argparse
import imaplib
import json
import random
import sys
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

from legacy.mail.client import imap, send
from legacy.odoo.rpc import ROOT
from legacy.seed.history import generate
from legacy.traffic.rfq_mix import RFQ_INBOX, Outgoing, build_mix

LABELS = ROOT / "data" / "generated" / "rfq_labels.jsonl"
PURCHASING = "purchasing@apex.local"


def to_message(o: Outgoing, to: str = RFQ_INBOX) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((o.display, o.sender))
    msg["To"] = to
    msg["Subject"] = o.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=o.sender.rsplit("@", 1)[1])
    msg.set_content(o.text)
    if o.html:
        msg.add_alternative(o.html, subtype="html")
    for name, data, mime in o.attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg


def sent_count() -> int:
    return len(LABELS.read_text(encoding="utf-8").splitlines()) if LABELS.exists() else 0


def send_next(n: int, only: list[int] | None = None) -> list[dict[str, Any]]:
    """The next n emails of the mix, or (`only`) exactly these message numbers of it: a class's curated set."""
    mix, start, out = build_mix(), sent_count(), []
    LABELS.parent.mkdir(parents=True, exist_ok=True)
    picks = (
        [o for no in only for o in mix if o.label.get("msg_no") == no]
        if only
        else [mix[i % len(mix)] for i in range(start, start + n)]
    )
    for i, o in enumerate(picks, start):
        msg = to_message(o)
        send(o.sender, msg)
        label = {"message_id": msg["Message-ID"], "sent_at": datetime.now(UTC).isoformat(timespec="seconds"),
                 "cycle": i // len(mix) + 1, "from": o.sender, "subject": o.subject, **o.label}  # fmt: skip
        with LABELS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(label, ensure_ascii=False) + "\n")
        out.append(label)
        print(f"sent #{i + 1:3d} {o.label['intent']:<22} {o.sender:<45} {o.subject}")
    return out


def drip(per_hour: float, count: int | None) -> None:
    rng, sent = random.Random(), 0
    while True:
        send_next(1)
        sent += 1
        if count is not None and sent >= count:
            return
        gap = rng.expovariate(per_hour / 3600)
        print(f"  next email in {gap:.0f} s")
        time.sleep(gap)


def purchasing_history() -> int:
    """W18: Tom forwards the customer's whole drawing to a subcontractor / finisher. Idempotent."""
    h = generate()
    sup = {s.key: s for s in h.suppliers}
    nda = {c.key: c for c in h.customers if c.nda and c.duplicate_of is None}
    parts = sorted((p for p in h.parts.values() if p.customer_key in nda), key=lambda p: p.code)[:3]
    plan = [(sup["SUP-12"], parts[0], 50, "machining"), (sup["SUP-12"], parts[1], 120, "machining"),
            (sup["SUP-07"], parts[2], 200, "anodize")]  # fmt: skip
    m = imap(PURCHASING)
    sent = 0
    try:
        m.create("Sent")  # exists already on most servers; error ignored
    except imaplib.IMAP4.error:
        pass
    try:
        for s, p, qty, what in plan:
            subject = f"FW: RFQ {p.code} ({nda[p.customer_key].name})"
            m.select("Sent")
            _, found = m.search(None, "SUBJECT", f'"{p.code}"')
            if found[0]:
                continue
            ask = (f"can you machine {qty} pcs of the attached part for us? Price per piece and lead time please."
                   if what == "machining" else f"please quote clear anodizing for {qty} pcs of the attached part.")  # fmt: skip
            o = Outgoing(PURCHASING, "Tom (Purchasing, Apex)", subject,
                         f"Hi,\n\n{ask}\nDrawing attached as we received it from the customer.\n\nThanks,\nTom\n"
                         f"Purchasing, Apex Precision Components\n\n---------- Forwarded message ----------\n"
                         f"From: {nda[p.customer_key].email}\nSubject: RFQ {p.code}\n\nPlease quote the attached part.",
                         None, [(f"{p.code}.pdf", (ROOT / "data/generated/drawings/history" / f"{p.code}.pdf").read_bytes(),
                                 "application/pdf")])  # fmt: skip
            msg = to_message(o, to=s.email)
            send(PURCHASING, msg)
            m.append("Sent", r"(\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            sent += 1
    finally:
        m.logout()
    print(f"purchasing history (W18): {sent} forwards sent, {len(plan) - sent} already there")
    return sent


def main() -> None:
    # Subjects include Hindi/German/Spanish; a Windows console or pipe defaults to cp1252.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--burst", type=int, metavar="N")
    g.add_argument("--only", metavar="1,2,3", help="exactly these message numbers of the mix (a class's curated set)")
    g.add_argument("--drip", action="store_true")
    g.add_argument("--purchasing-history", action="store_true")
    ap.add_argument("--per-hour", type=float, default=30.0)
    ap.add_argument("--count", type=int)
    a = ap.parse_args()
    if a.burst:
        send_next(a.burst)
    elif a.only:
        send_next(0, [int(x) for x in a.only.split(",")])
    elif a.drip:
        drip(a.per_hour, a.count)
    else:
        purchasing_history()


if __name__ == "__main__":
    main()
