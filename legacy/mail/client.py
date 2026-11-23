"""Who has a mailbox, their passwords, and real TLS connections to the mail server (stdlib only).

Used by setup_mail, the RFQ traffic generator, the responders (inside containers) and the tests.
Host side talks to 127.0.0.1:587/993; containers set MAIL_HOST=mail.apex.local.
"""

import email
import imaplib
import os
import smtplib
import ssl
import sys
import time
from collections.abc import Callable
from email.message import EmailMessage, Message
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from legacy.odoo.rpc import ROOT, env
from legacy.seed.history import History, generate, slug

MAIL_HOST = os.environ.get("MAIL_HOST", "127.0.0.1")
CA_FILE = Path(os.environ.get("MAIL_CA_FILE", ROOT / "data" / "generated" / "certs" / "ca.crt"))
STAFF = ["rfq@apex.local", "estimator1@apex.local", "senior@apex.local", "purchasing@apex.local"]
# Outside senders that are neither customers nor suppliers (a job applicant, a stranger for the attacks).
OTHERS = ["applicant@customers.local", "unknown.sender@customers.local"]


def prospect_email(name: str) -> str:
    return f"{slug(name)}@customers.local"


def mailboxes(h: History | None = None) -> list[str]:
    from legacy.seed.rfq_parts import PROSPECTS

    h = h or generate()
    custs = {c.email for c in h.customers} | {prospect_email(n) for _, n, *_ in PROSPECTS}
    return STAFF + sorted(custs) + OTHERS + [s.email for s in h.suppliers]


def password_key(address: str) -> str:
    if address == "rfq@apex.local":
        return "MAIL_RFQ_PASSWORD"  # W3: the one password every estimator shares
    domain = address.rsplit("@", 1)[1]
    return {"apex.local": "MAIL_STAFF_PASSWORD", "customers.local": "MAIL_CUSTOMERS_PASSWORD",
            "suppliers.local": "MAIL_SUPPLIERS_PASSWORD"}[domain]  # fmt: skip


def password(address: str) -> str:
    return env(password_key(address))


def tls() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(CA_FILE))


def send(sender: str, msg: EmailMessage) -> None:
    """Authenticated SMTP submission (port 587, STARTTLS) as `sender`."""
    with smtplib.SMTP(MAIL_HOST, 587, timeout=60) as s:
        s.starttls(context=tls())
        s.login(sender, password(sender))
        s.send_message(msg)


def imap(address: str) -> imaplib.IMAP4_SSL:
    m = imaplib.IMAP4_SSL(MAIL_HOST, 993, ssl_context=tls(), timeout=60)
    m.login(address, password(address))
    return m


def fetch(m: imaplib.IMAP4_SSL, uid: bytes) -> Message:
    _, data = m.uid("fetch", uid.decode(), "(BODY.PEEK[])")
    raw = next(part[1] for part in data if isinstance(part, tuple))
    return email.message_from_bytes(raw)


def text_of(msg: Message) -> str:
    """The plain-text body (falls back to HTML with tags left in)."""
    parts = [p for p in msg.walk() if p.get_content_maintype() == "text" and not p.get_filename()]
    part = next((p for p in parts if p.get_content_subtype() == "plain"), parts[0] if parts else None)
    if part is None:
        return ""
    payload = part.get_payload(decode=True)
    return payload.decode(part.get_content_charset() or "utf-8", "replace") if isinstance(payload, bytes) else ""


def reply(original: Message, sender: str, display: str, text: str,
          attachments: list[tuple[str, bytes, str]] | None = None) -> EmailMessage:  # fmt: skip
    """Answer `original` as `sender`, threaded (In-Reply-To / References), over authenticated SMTP."""
    msg = EmailMessage()
    msg["From"] = formataddr((display, sender))
    msg["To"] = original.get("Reply-To") or original["From"]
    subject = str(original.get("Subject", ""))
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[1])
    if original.get("Message-ID"):
        msg["In-Reply-To"] = original["Message-ID"]
        msg["References"] = f"{original.get('References', '')} {original['Message-ID']}".strip()
    msg.set_content(text)
    for name, data, mime in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    send(sender, msg)
    return msg


def handle_unseen_from_apex(address: str, handle: Callable[[Message], None]) -> int:
    """Run `handle` on each unread email from apex.local in `address`'s inbox; mark it read only after
    `handle` succeeded, so a failed reply is retried on the next poll."""
    m = imap(address)
    try:
        m.select("INBOX")
        _, data = m.uid("search", "UNSEEN", "FROM", "@apex.local")
        uids = data[0].split()
        for uid in uids:
            handle(fetch(m, uid))
            m.uid("store", uid.decode(), "+FLAGS", r"(\Seen)")
        return len(uids)
    finally:
        m.logout()


def poll_forever(name: str, once: Callable[[], int], interval: float) -> None:
    """Run `once` every `interval` seconds; survive a mail server or Odoo that is (re)starting."""
    sys.stdout.reconfigure(line_buffering=True, errors="replace")  # type: ignore[union-attr]
    print(f"{name}: watching mailboxes every {interval:.0f} s", flush=True)
    while True:
        try:
            once()
        except (OSError, imaplib.IMAP4.error, smtplib.SMTPException, RuntimeError) as e:
            print(f"{name}: waiting ({type(e).__name__}: {e})")
        time.sleep(interval)
