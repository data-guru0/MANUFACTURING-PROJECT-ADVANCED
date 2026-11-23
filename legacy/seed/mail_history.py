"""Three years of rfq@ email, so the baseline can be measured from the mailbox (as at a real client).

For every historical quote (HIST-0001 … in Odoo):
- the customer's RFQ goes into rfq@'s **Archive** folder, dated when it arrived;
- the quotation Apex emailed back goes into rfq@'s **Sent** folder, dated when it was sent.
Messages are written with IMAP APPEND (INTERNALDATE = the historical time) and carry the customer's
reference, which is how an estimator matches mail to the ERP today. Idempotent: refs already in a
folder are skipped.

ponytail: no drawings attached to the archived RFQs (they are already on the Odoo products) and
history times are naive UTC, the same clock Odoo stores them in.

Usage: uv run python -m legacy.seed.mail_history
"""

import imaplib
import re
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid

from legacy.mail.client import imap
from legacy.odoo.rpc import Odoo, env
from legacy.seed.history import generate

RFQ = "rfq@apex.local"
REF = re.compile(rb"HIST-\d{4}")


def _refs_in(m: imaplib.IMAP4_SSL, folder: str) -> set[str]:
    m.select(folder, readonly=True)
    _, data = m.search(None, "ALL")
    if not data[0]:
        return set()
    _, got = m.fetch(b",".join(data[0].split()).decode(), "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
    return {r.decode() for part in got if isinstance(part, tuple) for r in REF.findall(part[1])}


def _append(m: imaplib.IMAP4_SSL, folder: str, msg: EmailMessage, when: datetime) -> None:
    typ, _ = m.append(folder, r"(\Seen)", imaplib.Time2Internaldate(when.replace(tzinfo=UTC).timestamp()),
                      msg.as_bytes())  # fmt: skip
    if typ != "OK":
        raise RuntimeError(f"APPEND to {folder} failed")


def main() -> None:
    t0 = time.time()
    h = generate()
    cust = {c.key: c for c in h.customers}
    parts = h.parts
    o = Odoo("office", env("ODOO_OFFICE_PASSWORD"))
    names = {r["client_order_ref"]: r["name"] for r in o.call("sale.order", "search_read",
             [("client_order_ref", "=like", "HIST-%")], fields=["client_order_ref", "name"])}  # fmt: skip
    m = imap(RFQ)
    added = 0
    try:
        for folder in ("Archive", "Sent"):
            typ, _ = m.select(folder, readonly=True)
            if typ != "OK":
                m.create(folder)
            m.subscribe(folder)  # webmail (Roundcube) only lists subscribed folders
        have_rfq, have_sent = _refs_in(m, "Archive"), _refs_in(m, "Sent")
        for q in h.quotes:
            c, p = cust[q.partner_key], parts[q.part_code]
            rfq_id = make_msgid(domain="customers.local")
            if q.ref not in have_rfq:
                msg = EmailMessage()
                msg["From"], msg["To"] = formataddr((c.name, c.email)), RFQ
                msg["Subject"] = f"RFQ {q.part_code} - our ref {q.ref}"
                msg["Date"], msg["Message-ID"] = format_datetime(q.received_at.replace(tzinfo=UTC)), rfq_id
                msg.set_content(f"Hello,\n\nplease quote {q.qty} pcs of {p.code} ({p.label}), drawing attached.\n\n"
                                f"Regards,\n{c.name}")  # fmt: skip
                _append(m, "Archive", msg, q.received_at)
                added += 1
            if q.ref not in have_sent and q.ref in names:
                msg = EmailMessage()
                msg["From"], msg["To"] = formataddr(("Apex Precision Components", RFQ)), c.email
                msg["Subject"] = f"Quotation {names[q.ref]} - your ref {q.ref}"
                msg["Date"] = format_datetime(q.sent_at.replace(tzinfo=UTC))
                msg["Message-ID"], msg["In-Reply-To"] = make_msgid(domain="apex.local"), rfq_id
                msg.set_content(f"Dear customer,\n\nthank you for your enquiry. For {q.qty} pcs of {p.code} we offer "
                                f"{q.unit_price:.2f} USD per piece, delivery about 3 weeks after order.\n\n"
                                "Best regards,\nApex Precision Components")  # fmt: skip
                _append(m, "Sent", msg, q.sent_at)
                added += 1
    finally:
        m.logout()
    print(f"mail history: {len(h.quotes)} RFQs in rfq@/Archive, quotes in rfq@/Sent ({added} added) "
          f"in {time.time() - t0:.0f}s")  # fmt: skip


if __name__ == "__main__":
    main()
