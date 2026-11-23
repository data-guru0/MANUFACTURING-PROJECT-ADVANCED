"""Mailboxes and DKIM for the three mail domains (idempotent).

  --prestart  (before `docker compose up`, no container needed)
      1. the lab CA + TLS certificate (certs.py);
      2. data/generated/mail/postfix-accounts.cf: one line per mailbox, docker-mailserver's
         documented file provisioner. The server refuses to start with zero accounts, so this
         has to exist first. Existing hashes are kept, new mailboxes added, retired ones removed.
  (default, after up)
      3. DKIM keys for apex.local, customers.local, suppliers.local via the image's own
         `setup config dkim`, then one container re-create so OpenDKIM starts signing;
      4. if rfq@'s inbox is empty (a fresh mail volume), start a fresh RFQ label log.

ponytail: {SSHA512} hashes (stdlib); `setup email add` would use SHA512-CRYPT. Fine for long random
lab passwords; upgrade to ARGON2ID if real people ever pick these passwords.
"""

import argparse
import base64
import hashlib
import imaplib
import os
import subprocess
import time

from legacy.mail import certs
from legacy.mail.client import imap, mailboxes, password
from legacy.odoo.rpc import ROOT

ACCOUNTS = ROOT / "data" / "generated" / "mail" / "postfix-accounts.cf"
LABELS = ROOT / "data" / "generated" / "rfq_labels.jsonl"
MASTERS = ROOT / "data" / "generated" / "mail" / "dovecot-masters.cf"
DOMAINS = ["apex.local", "customers.local", "suppliers.local"]
COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(ROOT / "docker-compose.legacy.yml")]


def ssha512(secret: str) -> str:
    salt = os.urandom(16)
    return "{SSHA512}" + base64.b64encode(hashlib.sha512(secret.encode() + salt).digest() + salt).decode()


def write_accounts() -> int:
    """Keep the accounts file in sync with mailboxes(): keep existing hashes, add new, drop retired."""
    ACCOUNTS.parent.mkdir(parents=True, exist_ok=True)
    wanted = mailboxes()
    old = ACCOUNTS.read_text(encoding="utf-8").splitlines() if ACCOUNTS.exists() else []
    keep = [line for line in old if line.split("|", 1)[0] in wanted]
    have = {line.split("|", 1)[0] for line in keep}
    new = [f"{a}|{ssha512(password(a))}" for a in wanted if a not in have]
    if new or keep != old or not ACCOUNTS.exists():
        ACCOUNTS.write_text("\n".join(keep + new) + "\n", encoding="utf-8", newline="\n")
    return len(new)


def write_masters() -> bool:
    """S2: Dovecot master user `agent` (the FDE agent reads rfq@ as "rfq@apex.local*agent").
    Empty file when MAIL_AGENT_PASSWORD isn't set: the legacy system runs without the agent.
    ponytail: a master user could open ANY mailbox; our code and OPA restrict it to rfq@. Upgrade:
    a Dovecot ACL shared folder granting agent@ only lookup/read/keyword rights on rfq@ (S9 report)."""
    from legacy.odoo.rpc import env

    try:
        secret = env("MAIL_AGENT_PASSWORD")
    except KeyError:
        secret = ""
    keep = MASTERS.read_text(encoding="utf-8").strip() if MASTERS.exists() else ""
    line = keep if keep.startswith("agent|") and secret else (f"agent|{ssha512(secret)}" if secret else "")
    if line != keep or not MASTERS.exists():
        MASTERS.write_text(line + "\n" if line else "", encoding="utf-8", newline="\n")
    return bool(line)


def mail_exec(*args: str) -> str:
    return subprocess.run([*COMPOSE, "exec", "-T", "mail", *args], check=True, capture_output=True, text=True).stdout


def wait_for_login(timeout: float = 180) -> None:
    end = time.time() + timeout
    while True:
        try:
            imap("rfq@apex.local").logout()
            return
        except (OSError, imaplib.IMAP4.error):  # server starting, or accounts file not picked up yet
            if time.time() > end:
                raise
            time.sleep(3)


def ensure_dkim() -> bool:
    have = mail_exec("sh", "-c", "ls /tmp/docker-mailserver/opendkim/keys 2>/dev/null || true").split()
    if not all(d in have for d in DOMAINS):
        mail_exec("setup", "config", "dkim", "domain", ",".join(DOMAINS))
    elif mail_exec("sh", "-c", "wc -c < /etc/opendkim/KeyTable").strip() != "0":
        return False  # generated and installed
    # A plain restart skips the image's setup routines; only a recreated container installs the keys.
    subprocess.run([*COMPOSE, "up", "-d", "--wait", "--force-recreate", "mail"], check=True, capture_output=True)
    return True


def reset_labels_if_mailbox_fresh() -> bool:
    m = imap("rfq@apex.local")
    try:
        _, data = m.select("INBOX", readonly=True)
        empty = data[0] == b"0"
    finally:
        m.logout()
    if empty and LABELS.exists() and LABELS.stat().st_size:
        LABELS.write_text("", encoding="utf-8")
        return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prestart", action="store_true", help="certs + accounts file, before compose up")
    a = ap.parse_args()
    if a.prestart:
        certs.main()
        n = write_accounts()
        master = write_masters()
        print(f"mail accounts: {len(mailboxes())} mailboxes ({n} new) in {ACCOUNTS.relative_to(ROOT)}"
              f"{'; master user agent' if master else ''}")  # fmt: skip
        return
    wait_for_login()
    dkim = ensure_dkim()
    wait_for_login()
    fresh = reset_labels_if_mailbox_fresh()
    print(f"mail: {len(mailboxes())} mailboxes log in; DKIM keys {'created, server re-created' if dkim else 'present'}"
          f"{'; fresh mailbox, RFQ label log reset' if fresh else ''}")  # fmt: skip


if __name__ == "__main__":
    main()
