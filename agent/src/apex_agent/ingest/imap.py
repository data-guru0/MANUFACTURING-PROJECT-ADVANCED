"""The ingest service: rfq@ inbox -> MinIO + Postgres, one job per email (S3, W1).

- Logs in with the agent's OWN account (Dovecot master user: rfq@apex.local*agent), never rfq@'s password.
- Waits with IMAP IDLE and also polls every APEX_MAIL_POLL_S seconds (the fallback if IDLE misses something).
- Reads with BODY.PEEK, so the humans' unread/read state in Roundcube does not change. The only mark it
  leaves is the IMAP keyword $ApexIngested; nothing is ever moved or deleted.
- At-least-once + idempotent = exactly once: the keyword is set only AFTER the database commit, and a
  message seen again (keyword lost, re-run with --all) is recognised by its Message-ID.

Usage: python -m apex_agent.ingest.imap            # the long-running service
       python -m apex_agent.ingest.imap --once     # ingest what is new, print a JSON summary, exit
       python -m apex_agent.ingest.imap --once --all   # look at EVERY message again (proves de-duplication)
"""

import argparse
import email
import imaplib
import json
import re
import select
import ssl
import time
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

import psycopg
from minio.error import S3Error

from apex_agent.ingest.store import Store, sha256
from apex_agent.knowledge import db
from apex_agent.security.pii import log
from apex_agent.settings import MailSettings, load_as

KEYWORD = "$ApexIngested"
SCHEMA = Path(__file__).with_name("schema.sql")


def connect(ms: MailSettings) -> imaplib.IMAP4_SSL:
    m = imaplib.IMAP4_SSL(ms.host, ms.port, ssl_context=ssl.create_default_context(cafile=ms.ca_file))
    m.login(ms.user, ms.password.get_secret_value())
    m.select(ms.mailbox)  # read-write: needed to set the keyword (BODY.PEEK still leaves \Seen alone)
    return m


def _ids(value: str) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value)


def ingest_message(conn: psycopg.Connection[Any], store: Store, raw: bytes, mailbox: str, uid: int | None) -> tuple[int, bool]:
    """(email id, True if new). Stores the raw mail and its attachments, creates the email's one job."""
    raw_sha = sha256(raw)
    msg = email.message_from_bytes(raw, policy=policy.default)
    message_id = str(msg.get("Message-ID", "")).strip() or f"<sha256-{raw_sha}@apex.ingest>"
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM emails WHERE message_id = %s", (message_id,))
        if row := cur.fetchone():
            return int(row[0]), False
        store.put(f"raw/{raw_sha}.eml", raw, "message/rfc822")
        files = []
        for part in msg.iter_attachments():
            data = part.get_payload(decode=True)
            if not isinstance(data, bytes):
                continue
            h = sha256(data)
            store.put(f"incoming/{h}", data)
            files.append((part.get_filename() or "unnamed", part.get_content_type(), h, len(data)))
        name, addr = parseaddr(str(msg.get("From", "")))
        refs = _ids(str(msg.get("In-Reply-To") or "")) + _ids(str(msg.get("References") or ""))
        thread_id = None
        if refs:  # thread detection: the earliest known email this one answers
            cur.execute("SELECT coalesce(thread_id, id) FROM emails WHERE message_id = ANY(%s) ORDER BY id LIMIT 1", (refs,))
            thread_id = r[0] if (r := cur.fetchone()) else None
        duplicate_of = None
        if files:  # same sender, same attachments (by hash) = a re-sent RFQ, even with a new Message-ID
            cur.execute(
                """SELECT e.id FROM emails e JOIN attachments a ON a.email_id = e.id AND a.parent_id IS NULL
                   WHERE e.from_addr = %s GROUP BY e.id HAVING array_agg(a.sha256 ORDER BY a.sha256) = %s
                   ORDER BY e.id LIMIT 1""",
                (addr.lower(), sorted(f[2] for f in files)),
            )
            duplicate_of = r[0] if (r := cur.fetchone()) else None
        try:
            sent_at = parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else None
        except (TypeError, ValueError):
            sent_at = None
        cur.execute(
            """INSERT INTO emails (message_id, raw_sha256, mailbox, imap_uid, from_addr, from_name, subject, sent_at,
                                   in_reply_to, thread_id, duplicate_of)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (message_id, raw_sha, mailbox, uid, addr.lower(), name, str(msg.get("Subject", "")), sent_at,
             refs[0] if refs else None, thread_id, duplicate_of),
        )  # fmt: skip
        email_id = int(cur.fetchone()[0])  # type: ignore[index]
        for fname, ctype, h, size in files:
            cur.execute("INSERT INTO attachments (email_id, filename, claimed_type, sha256, size) VALUES (%s,%s,%s,%s,%s)",
                        (email_id, fname, ctype, h, size))  # fmt: skip
        cur.execute("INSERT INTO jobs (email_id) VALUES (%s) ON CONFLICT (email_id) DO NOTHING", (email_id,))
    conn.commit()
    return email_id, True


def ingest_new(
    m: imaplib.IMAP4_SSL, conn: psycopg.Connection[Any], store: Store, mailbox: str, every: bool = False
) -> dict[str, int]:
    _, data = m.uid("SEARCH", "ALL" if every else f"UNKEYWORD {KEYWORD}")
    new = seen = 0
    for uid in data[0].split():
        _, d = m.uid("FETCH", uid, "(BODY.PEEK[])")
        raw = next(p[1] for p in d if isinstance(p, tuple))
        email_id, created = ingest_message(conn, store, raw, mailbox, int(uid))
        m.uid("STORE", uid, "+FLAGS.SILENT", f"({KEYWORD})")  # only after the commit above
        new, seen = new + created, seen + (not created)
        if created:
            log("ingested", email_id=email_id, uid=int(uid))
    return {"new": new, "already_ingested": seen}


def idle(m: imaplib.IMAP4_SSL, timeout: float) -> None:
    """Wait until the server says something changed (IMAP IDLE, RFC 2177) or `timeout` seconds pass.
    ponytail: hand-rolled on imaplib internals (Python 3.14 adds IMAP4.idle()); the poll fallback covers any miss."""
    tag: bytes = m._new_tag()
    m.send(tag + b" IDLE\r\n")
    if not m.readline().startswith(b"+"):
        return
    if select.select([m.sock], [], [], timeout)[0]:
        m.readline()  # e.g. "* 65 EXISTS"
    m.send(b"DONE\r\n")
    while not m.readline().startswith(tag):
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--all", action="store_true", help="re-check every message, not only the ones without the keyword")
    a = ap.parse_args()
    ms, store = load_as(MailSettings), Store()
    conn = db.connect(SCHEMA)
    if a.once:
        m = connect(ms)
        print(json.dumps(ingest_new(m, conn, store, ms.mailbox, a.all)))
        m.logout()
        return
    while True:  # the service: reconnect on any error, never exit
        try:
            m = connect(ms)
            log("ingest connected", mailbox=ms.mailbox)
            while True:
                res = ingest_new(m, conn, store, ms.mailbox)
                if res["new"]:
                    log("ingest round", **res)
                idle(m, ms.poll_s)
        except (OSError, imaplib.IMAP4.error, psycopg.Error, S3Error) as e:
            log("ingest error, reconnecting in 5 s", error=f"{type(e).__name__}: {e}")
            if conn.closed:
                conn = db.connect(SCHEMA)
            else:
                conn.rollback()
            time.sleep(5)


if __name__ == "__main__":
    main()
