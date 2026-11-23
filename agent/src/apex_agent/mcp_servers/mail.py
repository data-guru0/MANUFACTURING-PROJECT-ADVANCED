"""mail-mcp (S8): the ONLY way the agent sends email. One tool, `send_reply`, as rfq@apex.local.

It logs in with the agent's own Dovecot master login (rfq@apex.local*agent: the mail server records it as `agent`),
never the shared rfq@ password. Two locks: OPA's tool policy (only the `act` node) and the person's signed approval of
exactly this email: recipient, subject and body are hashed HERE and must match the approval (OPA apex/actions).

Idempotent: the Message-ID is derived from the job and the approved text, and a reply already in rfq@'s Sent folder is
never sent again. The reply threads under the customer's RFQ (In-Reply-To / References) and a copy is filed in Sent,
so the estimators see it in Roundcube like any reply they wrote themselves.
"""

import base64
import imaplib
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from apex_agent.actions.approval import text_sha256
from apex_agent.mcp_servers.common import guard, require_approval, serve
from apex_agent.settings import MailSettings, load_as

mcp: MCPServer[Any] = MCPServer("mail-mcp", instructions="Send an approved reply from rfq@apex.local.")
FROM = "Apex Precision Components <rfq@apex.local>"
SENT = "Sent"


def _imap(s: MailSettings, tls: ssl.SSLContext) -> imaplib.IMAP4_SSL:
    m = imaplib.IMAP4_SSL(s.host, s.port, ssl_context=tls)
    m.login(s.user, s.password.get_secret_value())
    return m


@mcp.tool()
def send_supplier_rfq(email_id: int, rfq_id: int, approval: str, to: str, subject: str, body: str, ctx: Context[Any, Any],
                      attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    """S17: a NEW email (not a reply) to a supplier, exactly as a person approved it (OPA: action supplier_rfq, the
    same text hash, not for an NDA-restricted customer or an export-controlled job). Idempotent by Message-ID."""
    guard(ctx, "mail.send_supplier_rfq")
    sha = text_sha256(to, subject, body)
    require_approval("mail.send_supplier_rfq", email_id, approval, text_sha256=sha)
    if not to.lower().endswith(
        "@suppliers.local"
    ):  # ponytail: the simulation's supplier domain; Odoo vendors' domains in a real shop
        raise ToolError("a supplier RFQ goes to a supplier address only")
    s = load_as(MailSettings)
    tls = ssl.create_default_context(cafile=s.ca_file)
    message_id = f"<apex-srfq-{rfq_id}-{sha[:16]}@apex.local>"
    m = _imap(s, tls)
    try:
        m.select(SENT)
        typ, found = m.search(None, "HEADER", "Message-ID", message_id)
        if typ == "OK" and found and found[0].split():
            return {"sent": False, "already_sent": True, "message_id": message_id}
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = FROM, to, subject
        msg["Message-ID"], msg["Date"] = message_id, formatdate(localtime=True)
        msg.set_content(body)
        for a in attachments or []:
            maintype, _, subtype = (a.get("mimetype") or "application/octet-stream").partition("/")
            msg.add_attachment(
                base64.b64decode(a["data_b64"], validate=True), maintype=maintype, subtype=subtype, filename=a["name"]
            )
        try:
            with smtplib.SMTP(s.host, 587, timeout=60) as smtp:
                smtp.starttls(context=tls)
                smtp.login(s.user, s.password.get_secret_value())
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            raise ToolError(f"the mail server did not accept the RFQ: {e}") from e
        m.append(SENT, "\\Seen", None, msg.as_bytes())
    finally:
        m.logout()
    return {"sent": True, "message_id": message_id}


@mcp.tool()
def send_reply(email_id: int, approval: str, to: str, subject: str, body: str, ctx: Context[Any, Any], in_reply_to: str = "",
               references: str = "", attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    """Send the approved reply to `to`, threaded under `in_reply_to`, with attachments [{name, mimetype, data_b64}]."""
    guard(ctx, "mail.send_reply")
    sha = text_sha256(to, subject, body)
    require_approval("mail.send_reply", email_id, approval, text_sha256=sha)
    s = load_as(MailSettings)
    tls = ssl.create_default_context(cafile=s.ca_file)
    message_id = f"<apex-reply-{email_id}-{sha[:16]}@apex.local>"
    m = _imap(s, tls)
    try:
        m.select(SENT)
        typ, found = m.search(None, "HEADER", "Message-ID", message_id)
        if typ == "OK" and found and found[0].split():
            return {"sent": False, "already_sent": True, "message_id": message_id}
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = FROM, to, subject
        msg["Message-ID"], msg["Date"] = message_id, formatdate(localtime=True)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = f"{references} {in_reply_to}".strip()
        msg.set_content(body)
        for a in attachments or []:
            maintype, _, subtype = (a.get("mimetype") or "application/octet-stream").partition("/")
            msg.add_attachment(
                base64.b64decode(a["data_b64"], validate=True), maintype=maintype, subtype=subtype, filename=a["name"]
            )
        try:
            with smtplib.SMTP(s.host, 587, timeout=60) as smtp:
                smtp.starttls(context=tls)
                smtp.login(s.user, s.password.get_secret_value())
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            raise ToolError(f"the mail server did not accept the reply: {e}") from e
        # ponytail: a crash between sending and filing the copy would send it again on retry; a Postfix BCC-to-Sent
        # rule (or the mail server's own sent-copy) closes that gap.
        m.append(SENT, "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    finally:
        m.logout()
    return {"sent": True, "message_id": message_id, "to": to}


def main() -> None:
    serve(mcp, "mail-mcp")


if __name__ == "__main__":
    main()
