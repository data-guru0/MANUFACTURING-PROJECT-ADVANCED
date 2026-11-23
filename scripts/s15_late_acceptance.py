"""S15 test traffic: ONE synthetic, tagged customer email accepting a legacy quotation after its validity date.

No open quotation in Odoo has expired yet on the day the stack is seeded, so this simulates the customer answering a week
after the validity date: the email's Date header is the validity date + 6 days. It goes through the real mail server
to rfq@, and the agent handles it like any other reply (ingest -> gate -> triage -> reply_router). Tagged with
X-Apex-Synthetic so it can never be mistaken for a real customer.

Usage: uv run python -m scripts.s15_late_acceptance <quotation> <customer mailbox> <company> <valid until>
  e.g. S00388 delmontconveyorsyste@customers.local "Delmont Conveyor Systems" 2026-11-25
"""

import sys
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from legacy.mail.client import send


def main() -> None:
    quotation, sender, company, valid_until = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    when = datetime.combine(date.fromisoformat(valid_until) + timedelta(days=6), time(9, 30)).astimezone()
    msg = EmailMessage()
    msg["From"] = f"{company} <{sender}>"
    msg["To"] = "rfq@apex.local"
    msg["Subject"] = f"Re: Quotation {quotation}"
    msg["Date"] = format_datetime(when)
    msg["Message-ID"] = make_msgid(domain="customers.local")
    msg["X-Apex-Synthetic"] = "S15 late-acceptance test"
    msg.set_content(f"Hello,\n\nwe accept your quotation {quotation}. Our purchase order follows by separate mail.\n\n"
                    f"Regards,\n{company}")  # fmt: skip
    send(sender, msg)
    print(f"sent: {company} accepts {quotation}, dated {when.date()} (valid until {valid_until})")


if __name__ == "__main__":
    main()
