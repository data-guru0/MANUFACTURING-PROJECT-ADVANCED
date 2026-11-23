"""S3 Done-when, against the live stacks (make demo-agent; the ingest and worker services running):
- the labelled traffic produces one job per email, with no duplicates on re-run;
- the EICAR test file is quarantined;
- triage and injection-detection accuracy on the labels are reported (docs/fde/TRIAGE_EVAL.md).
Plus: the humans' inbox is unchanged (no \\Seen, nothing moved), threads and re-sent RFQs are recognised,
a flagged email is still processed (red banner + human), and personal data is redacted in the logs."""

import json
import ssl
import subprocess
import time
import uuid
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any

import pytest

from legacy.mail.client import send
from legacy.odoo.rpc import env

REPO = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(REPO / "docker-compose.legacy.yml"), "-f",
           str(REPO / "docker-compose.agent.yml")]  # fmt: skip
# The industry-standard antivirus test string, assembled at run time so no file on this PC contains it
# (Windows Defender would quarantine this test file itself).
EICAR = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*").encode()
SENDER = "unknown.sender@customers.local"


def sql(query: str, *params: str) -> list[list[str]]:
    for p in params:  # tiny literal substitution for the test's own values (no user input)
        query = query.replace("%s", "'" + p.replace("'", "''") + "'", 1)
    r = subprocess.run([*COMPOSE, "exec", "-T", "app-postgres", "psql", "-U", "postgres", "-d", "apex", "-At", "-F", "\t", "-c", query],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)  # fmt: skip
    assert r.returncode == 0, r.stderr
    return [line.split("\t") for line in r.stdout.splitlines()]


def in_container(service: str, *args: str, timeout: int = 300) -> str:
    r = subprocess.run([*COMPOSE, "exec", "-T", service, *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)  # fmt: skip
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    return r.stdout


def mail(subject: str, text: str, attachments: list[tuple[str, bytes, str]] = (), **headers: str) -> str:  # type: ignore[assignment]
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = f"S3 test <{SENDER}>", "rfq@apex.local", subject
    msg["Message-ID"] = make_msgid(domain="customers.local")
    for k, v in headers.items():
        msg[k.replace("_", "-")] = v
    msg.set_content(text)
    for name, data, mime in attachments:
        main, sub = mime.split("/")
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    send(SENDER, msg)
    return str(msg["Message-ID"])


def wait_triaged(message_id: str, timeout: float = 1200) -> dict[str, Any]:
    """The ingest service (IMAP IDLE) and the worker service do the work; the test only watches the database.
    The queue is first-in first-out, and from S4 each RFQ is also extracted: behind a burst it can take minutes."""
    end = time.time() + timeout
    while time.time() < end:
        rows = sql("""SELECT e.id, j.status, coalesce(g.triage, ''), coalesce(g.red_banner::text, ''), coalesce(g.reasons::text, '[]'),
                             coalesce(e.thread_id::text, ''), coalesce(e.duplicate_of::text, ''), extract(epoch FROM e.ingested_at)
                      FROM emails e JOIN jobs j ON j.email_id = e.id LEFT JOIN gate_results g ON g.email_id = e.id
                      WHERE e.message_id = %s""", message_id)  # fmt: skip
        if rows and rows[0][1] in (
            "triaged",
            "extracted",
            "estimated",
            "awaiting_review",
            "done",
            "failed",
        ):  # S6: an RFQ waits for review
            r = rows[0]
            return {"id": int(r[0]), "status": r[1], "triage": r[2], "red_banner": r[3] == "true", "reasons": json.loads(r[4]),
                    "thread_id": r[5], "duplicate_of": r[6], "ingested_at": float(r[7])}  # fmt: skip
        time.sleep(3)
    raise AssertionError(f"{message_id} was not processed within {timeout} s")


@pytest.fixture(scope="module")
def eicar_mail() -> tuple[str, dict[str, Any], float]:
    sent = time.time()
    mid = mail(f"RFQ with attachment {uuid.uuid4().hex[:6]}", "Please quote the attached part, 25 pcs.",
               [("drawing_notes.txt", EICAR, "text/plain")])  # fmt: skip
    return mid, wait_triaged(mid), sent


def test_eicar_is_quarantined_and_the_email_still_processed(eicar_mail: tuple[str, dict[str, Any], float]) -> None:
    mid, job, sent = eicar_mail
    assert job["ingested_at"] - sent < 60, "IMAP IDLE should pick a new email up within seconds"
    rows = sql("SELECT a.verdict, a.reasons::text, a.sha256 FROM attachments a JOIN emails e ON e.id = a.email_id WHERE e.message_id = %s",
               mid)  # fmt: skip
    assert len(rows) == 1 and rows[0][0] == "quarantined" and "virus found: Eicar" in rows[0][1], rows
    digest = rows[0][2]
    where = in_container("worker", "python", "-c", f"from apex_agent.ingest.store import Store; s = Store(); "
                         f"print([p for p in ('incoming', 'files', 'quarantine') if s._has(p + '/{digest}')])")  # fmt: skip
    assert "['quarantine']" in where, where
    # a flagged job is still processed: triaged, with a red banner (and therefore a human)
    assert (
        job["status"] in ("triaged", "extracted", "estimated", "awaiting_review")
        and job["red_banner"]
        and any("quarantined" in r for r in job["reasons"])
    )


def test_threads_and_resent_rfqs_are_recognised(eicar_mail: tuple[str, dict[str, Any], float]) -> None:
    first, job, _ = eicar_mail
    reply = mail("Re: our RFQ", "Any news on our quote?", In_Reply_To=first, References=first)
    resend = mail("RFQ again", "Sending this again.", [("drawing_notes.txt", EICAR, "text/plain")])
    r1, r2 = wait_triaged(reply), wait_triaged(resend)
    assert r1["thread_id"] == str(job["id"])  # In-Reply-To -> the first email of the conversation
    # same sender + same attachment hashes, new Message-ID -> the FIRST email that carried them (an earlier test run
    # may already have sent this exact file, and then the fixture's email is itself a duplicate of that one)
    assert r2["duplicate_of"] == (job["duplicate_of"] or str(job["id"]))


def test_one_job_per_email_and_no_duplicates_on_rerun() -> None:
    # --all looks at EVERY message in the inbox again, as if the $ApexIngested keywords were lost
    out = json.loads(in_container("ingest", "python", "-m", "apex_agent.ingest.imap", "--once", "--all").splitlines()[-1])
    assert out["new"] == 0 and out["already_ingested"] >= 64, out
    (emails, distinct, jobs, job_emails), = sql(
        "SELECT (SELECT count(*) FROM emails), (SELECT count(DISTINCT message_id) FROM emails), "
        "(SELECT count(*) FROM jobs), (SELECT count(DISTINCT email_id) FROM jobs)")  # fmt: skip
    assert emails == distinct == jobs == job_emails, (emails, distinct, jobs, job_emails)
    labels = [
        json.loads(x)["message_id"] for x in (REPO / "data/generated/rfq_labels.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    ids = {r[0] for r in sql("SELECT message_id FROM emails")}
    assert set(labels) <= ids, f"{len(set(labels) - ids)} labelled emails were not ingested"


def test_the_humans_inbox_is_unchanged() -> None:
    import imaplib

    ctx = ssl.create_default_context(cafile=str(REPO / "data" / "generated" / "certs" / "ca.crt"))
    m = imaplib.IMAP4_SSL("127.0.0.1", 993, ssl_context=ctx, timeout=60)
    m.login("rfq@apex.local*agent", env("MAIL_AGENT_PASSWORD"))
    m.select("INBOX", readonly=True)
    tagged = m.uid("SEARCH", "KEYWORD", "$ApexIngested")[1][0].split()
    seen_by_agent = m.uid("SEARCH", "KEYWORD", "$ApexIngested", "SEEN")[1][0].split()
    total = int(m.select("INBOX", readonly=True)[1][0] or 0)
    m.logout()
    stored = int(sql("SELECT count(*) FROM emails WHERE mailbox = 'INBOX'")[0][0])
    assert len(tagged) >= 64 and total >= stored  # every ingested email is still in the INBOX (nothing moved)
    # Nobody has opened the test-sent emails in Roundcube, so the agent reading them must not have marked them read.
    test_uids = {r[0] for r in sql("SELECT imap_uid::text FROM emails WHERE from_addr = %s", SENDER)}
    assert not test_uids & {u.decode() for u in seen_by_agent}, "BODY.PEEK must not set \\Seen"


def test_personal_data_is_redacted_in_logs() -> None:
    out = in_container("worker", "python", "-m", "apex_agent.security.pii")
    assert "<PERSON>" in out and "<EMAIL_ADDRESS>" in out
    logs = subprocess.run(
        ["docker", "logs", "apex-worker-1"], capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout
    logs += subprocess.run(
        ["docker", "logs", "apex-ingest-1"], capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout
    assert "@customers.local" not in logs and "@suppliers.local" not in logs, "an email address reached the logs"


@pytest.mark.llm
def test_triage_and_injection_detection_are_reported() -> None:
    subprocess.run([*COMPOSE, "run", "--rm", "-T", "worker", "python", "-m", "apex_agent.worker", "--drain"],
                   check=True, capture_output=True, timeout=1800)  # fmt: skip
    r = subprocess.run([*COMPOSE, "run", "--rm", "-T", "knowledge", "python", "-m", "apex_agent.evaluate_triage"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)  # fmt: skip
    assert r.returncode == 0, r.stderr[-2000:]
    res = json.loads(r.stdout.splitlines()[-1])
    print(f"\ntriage/gate: {res}")
    assert res["one_job_per_email"] and res["processed"] >= 64
    assert res["triage_accuracy"] >= 0.8  # a target, measured and reported in TRIAGE_EVAL.md
    assert res["injection_recall"] is not None
    report = (REPO / "docs" / "fde" / "TRIAGE_EVAL.md").read_text(encoding="utf-8")
    assert "Prompt-injection detection" in report and "Every attack in the mix" in report
