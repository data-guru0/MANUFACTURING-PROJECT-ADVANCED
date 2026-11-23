"""S6 Done-when: a real RFQ runs to the interrupt; the worker is killed and restarted, and the job resumes; the full
trace is visible in Langfuse. Plus: a human decision resumes the graph (Command(resume=...)), edits are stored as
feedback, and the faithfulness check catches invented numbers. Real services, real Groq, a real kill."""

import json
import ssl
import subprocess
import sys
import time
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any

import httpx
import pytest

from legacy.mail.client import send
from legacy.odoo.rpc import env

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "agent" / "src"))
from apex_agent.graph.draft import allowed_numbers, unfaithful  # noqa: E402

COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(REPO / "docker-compose.legacy.yml"), "-f",
           str(REPO / "docker-compose.agent.yml")]  # fmt: skip
TLS = ssl.create_default_context(cafile=str(REPO / "data" / "generated" / "certs" / "ca.crt"))
NODES = ["security_gate", "triage", "extract", "reconcile", "retrieve", "cost_part", "risk_policy", "draft_reply"]


def sql(query: str) -> list[list[str]]:
    r = subprocess.run([*COMPOSE, "exec", "-T", "app-postgres", "psql", "-U", "postgres", "-d", "apex", "-At", "-F", "\t", "-c", query],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)  # fmt: skip
    assert r.returncode == 0, r.stderr
    return [line.split("\t") for line in r.stdout.splitlines()]


def review(*args: str) -> dict[str, Any]:
    r = subprocess.run([*COMPOSE, "exec", "-T", "worker", "python", "-m", "apex_agent.graph.review", *args],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)  # fmt: skip
    assert r.returncode == 0, r.stderr[-2000:]
    out: dict[str, Any] = json.loads(r.stdout[r.stdout.index("{") :])
    return out


def wait(query: str, timeout: float, what: str) -> list[list[str]]:
    end = time.time() + timeout
    while time.time() < end:
        if rows := sql(query):
            return rows
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(scope="module")
def killed_job() -> dict[str, Any]:
    """A fresh RFQ with a real drawing (DXF + vector PDF of RFQ-003), killed right after the security gate."""
    gt = json.loads((REPO / "data/generated/ground_truth/RFQ-003.json").read_text(encoding="utf-8"))
    msg = EmailMessage()
    sender = "zephyrhvacproducts@customers.local"
    msg["From"], msg["To"], msg["Subject"] = f"Zephyr HVAC Products <{sender}>", "rfq@apex.local", f"RFQ {gt['part_code']}"
    msg["Message-ID"] = make_msgid(domain="customers.local")
    msg.set_content(f"Hello Apex team,\n\nplease quote {gt['part_code']}: 40 pcs and 120 pcs.\n\nRegards,\nZephyr HVAC Products")
    for kind, mime in (("dxf", ("image", "vnd.dxf")), ("pdf", ("application", "pdf"))):
        msg.add_attachment(
            (REPO / gt["files"][kind]).read_bytes(), maintype=mime[0], subtype=mime[1], filename=f"{gt['part_code']}.{kind}"
        )
    send(sender, msg)
    mid = str(msg["Message-ID"])
    (email_id,), *_ = wait(f"SELECT id FROM emails WHERE message_id = '{mid}'", 600, "ingestion")
    wait(f"SELECT 1 FROM graph_events WHERE email_id = {email_id} AND node = 'security_gate'", 1200, "the security gate")
    subprocess.run(["docker", "kill", "apex-worker-1"], check=True, capture_output=True)  # mid-job, not a polite stop
    killed_after = [r[0] for r in sql(f"SELECT node FROM graph_events WHERE email_id = {email_id} ORDER BY id")]
    status_when_killed = sql(f"SELECT status FROM jobs WHERE email_id = {email_id}")[0][0]
    subprocess.run(["docker", "start", "apex-worker-1"], check=True, capture_output=True)
    wait(f"SELECT 1 FROM jobs WHERE email_id = {email_id} AND status = 'awaiting_review'", 900, "the review interrupt")
    return {
        "email_id": int(email_id),
        "killed_after": killed_after,
        "status_when_killed": status_when_killed,
        "part": gt["part_code"],
    }


def test_a_killed_worker_resumes_the_job_at_the_next_node(killed_job: dict[str, Any]) -> None:
    e = killed_job["email_id"]
    assert killed_job["status_when_killed"] == "running" and "draft_reply" not in killed_job["killed_after"], killed_job
    counts = dict(sql(f"SELECT node, count(*) FROM graph_events WHERE email_id = {e} GROUP BY node"))
    assert {n: int(counts.get(n, 0)) for n in NODES} == dict.fromkeys(NODES, 1), counts  # nothing ran twice
    logs = subprocess.run(
        ["docker", "logs", "apex-worker-1"], capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout
    assert '"event": "worker started"' in logs and '"event": "graph resume"' in logs


def test_the_job_waits_at_the_interrupt_with_a_faithful_draft(killed_job: dict[str, Any]) -> None:
    shown = review("show", str(killed_job["email_id"]))
    assert shown["status"] == "awaiting_review" and shown["next"] == ["human_review"]
    pending = shown["waiting_for"][0]
    assert pending["route"] in ("quote", "clarify", "escalate")
    d = pending["draft"]
    assert d["faithful"] and d["customer_language_text"] and d["english_text"], d
    killed_job["trace_id"] = shown["trace_id"]


def test_the_full_trace_is_in_langfuse(killed_job: dict[str, Any]) -> None:
    trace = killed_job.get("trace_id") or review("show", str(killed_job["email_id"]))["trace_id"]
    auth = (env("LANGFUSE_PUBLIC_KEY"), env("LANGFUSE_SECRET_KEY"))
    url = f"https://localhost:8443/api/public/v2/observations?traceId={trace}&fields=core,basic"
    names: set[str] = set()
    end = time.time() + 180
    while (
        time.time() < end
        and not {"security_gate:quote-guard", "triage:quote-triage", "extractor:quote-reasoner", "drafter:quote-reasoner"}
        <= names
    ):
        names = {o["name"] for o in httpx.get(url, auth=auth, verify=TLS, timeout=30).json().get("data") or []}
        time.sleep(3)
    # one trace, across the kill: the gate's calls before it, extraction and drafting after it
    assert {"security_gate:quote-guard", "triage:quote-triage", "extractor:quote-reasoner", "drafter:quote-reasoner"} <= names, (
        names
    )


def test_a_human_decision_resumes_the_graph(killed_job: dict[str, Any]) -> None:
    e = killed_job["email_id"]
    with pytest.raises(AssertionError):
        review("reject", str(e), "--by", "Ramesh", "--role", "senior_estimator")  # a rejection needs a reason
    out = review(
        "approve", str(e), "--by", "Ramesh", "--role", "senior_estimator", "--reason", "checked the drawing and the price"
    )
    assert out["status"] == "done"
    assert sql(f"SELECT status FROM jobs WHERE email_id = {e}")[0][0] == "done"  # the queue mirrors the graph
    action, by = sql(f"SELECT action, decided_by FROM decisions WHERE email_id = {e}")[0]
    assert (action, by) == ("approve", "Ramesh")
    assert {r[0] for r in sql(f"SELECT node FROM graph_events WHERE email_id = {e}")} >= {"human_review", "act", "learn"}


def test_an_edit_is_stored_as_feedback() -> None:
    waiting = review("waiting")["awaiting_review"]
    assert waiting, "another job should be waiting for review"
    e = waiting[0]
    out = review(
        "edit",
        str(e),
        "--by",
        "Ramesh",
        "--role",
        "senior_estimator",
        "--reason",
        "warmer tone",
        "--text",
        "Dear customer, thank you for your RFQ.",
    )
    assert out["status"] == "done"
    assert sql(f"SELECT status FROM jobs WHERE email_id = {e}")[0][0] == "done"
    field, by, reason = sql(f"SELECT field, by_user, reason FROM feedback WHERE email_id = {e} ORDER BY id DESC LIMIT 1")[0]
    assert (field, by, reason) == ("customer_language_text", "Ramesh", "warmer tone")


def test_the_faithfulness_check_catches_invented_numbers() -> None:
    facts = {"parts": [{"part": "D-20016-A", "price_breaks": [{"quantity": 40, "unit_price": 18.25}]}], "committed": "2027-01-20"}
    nums, dates = allowed_numbers(facts, "please quote 40 pcs")
    assert unfaithful("40 Stück zu je 18,25 USD, Liefertermin 20.01.2027.", nums, dates) == []
    assert unfaithful("40 pcs at 17.95 USD, in 3 weeks, delivery 2027-01-27.", nums, dates) == ["2027-01-27", "17.95", "3"]
