"""The worker: takes jobs from the Postgres queue and runs each one through the quote graph (S6, graph/build.py).

Queue (ADR 0002): `FOR UPDATE SKIP LOCKED`, so several workers (`--scale worker=2`) never take the same job.
Durable execution: the graph is checkpointed in Postgres after every node. A worker that is killed mid-job takes
the job back when it restarts (jobs it had claimed, by its hostname) and the graph resumes at the next node.
Failures degrade honestly: ClamAV, LiteLLM, OPA or Odoo down -> the job goes back to the queue with a growing
delay (a provider's daily quota: as long as the provider says, at most 30 min; attempts not used up); after 3 attempts it is `failed` for a human.

Usage: python -m apex_agent.worker            # the long-running service
       python -m apex_agent.worker --drain    # process until the queue is empty, print a summary, exit
       python -m apex_agent.worker --redo     # every job again from the gate (e.g. after a policy change)
       python -m apex_agent.worker --redo-extraction   # RFQs again from extraction only
       python -m apex_agent.worker --retry-failed      # failed jobs back into the queue
       python -m apex_agent.worker --redo-estimates    # RFQs again from the estimate (S5 is code: no tokens)
"""

import argparse
import email
import json
import socket
import time
from email import policy
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from apex_agent import audit
from apex_agent.actions import outcomes
from apex_agent.costing import cycle_time, priority
from apex_agent.documents.extract import Extractor
from apex_agent.governance import canary, costs
from apex_agent.graph.build import QuoteGraph
from apex_agent.ingest.imap import SCHEMA
from apex_agent.ingest.store import Store
from apex_agent.knowledge import db
from apex_agent.llm.client import AiDisabled, AiUnavailable, LLMClient, NeedsHuman, NotAllowed, QuotaExhausted
from apex_agent.replies import tool as reply_tool
from apex_agent.security import content
from apex_agent.security.files import ScannerDown
from apex_agent.security.gate import Gate
from apex_agent.security.pii import log
from apex_agent.settings import DbSettings, DocSettings, GateSettings, load, load_as
from apex_agent.suppliers import tick as supplier_tick
from apex_agent.triage import triage

MAX_ATTEMPTS = 3
OUTCOMES_EVERY_S = 120  # S8: how often learn reads the quotations' states back from Odoo
OUTAGE_WAIT_S = 60  # S19: a dependency is down (AI, Odoo, a tool server): look again in a minute, without burning attempts
OUTAGE_SIGNS = ("unreachable", "connection refused", "connecterror", "name or service not known", "temporarily unavailable",
                "timed out", "502", "503", "504")  # fmt: skip


def outage(e: BaseException) -> bool:
    """S19: is this a dependency being down (wait) rather than something wrong with the job (retry, then fail)?
    ponytail: a message heuristic for tool errors that arrive as text; typed errors where we raise them ourselves."""
    return isinstance(e, AiUnavailable | AiDisabled | OSError) or any(s in str(e).lower() for s in OUTAGE_SIGNS)


QUOTA_WAIT_S = 1800  # when the provider doesn't say when the quota frees up


def quota_wait(e: QuotaExhausted) -> int:
    """Wait as long as the provider says (+30 s), at least 1 min, at most QUOTA_WAIT_S."""
    return QUOTA_WAIT_S if e.retry_after_s is None else int(max(60, min(QUOTA_WAIT_S, e.retry_after_s + 30)))


HOST = socket.gethostname()  # compose sets a stable name (apex-worker): it survives restarts and recreated containers
CLAIM = """WITH j AS (SELECT id, status = 'sent' AS followup FROM jobs
                      WHERE (status IN ('queued', 'decided') AND available_at <= now())
                         OR (status = 'sent' AND followup_at <= now())        -- S15: a follow-up that has come due
                      ORDER BY status = 'decided' DESC, id FOR UPDATE SKIP LOCKED LIMIT 1)
           UPDATE jobs SET status = 'running', attempts = CASE WHEN j.followup THEN 1 ELSE jobs.attempts + 1 END, started_at = now(), error = NULL, locked_by = %s,
                           followup_at = CASE WHEN j.followup THEN NULL ELSE jobs.followup_at END
           FROM j WHERE jobs.id = j.id
           RETURNING jobs.id, jobs.email_id, jobs.attempts, jobs.decision, j.followup"""
# A worker that died mid-job leaves it `running`. On start it takes back its own; anyone's after 10 minutes.
RESCUE_MINE = "UPDATE jobs SET status = 'queued', available_at = now() WHERE status = 'running' AND locked_by = %s"
RESCUE = "UPDATE jobs SET status = 'queued' WHERE status = 'running' AND started_at < now() - interval '10 minutes'"
FINAL = ("triaged", "awaiting_review", "done", "sent", "manual", "failed")


class Worker:
    schema = SCHEMA

    def __init__(self) -> None:
        s, gs, ds = load(), load_as(GateSettings), load_as(DocSettings)
        self.conn = db.connect(SCHEMA)
        self.store = Store()
        self.opa_url = s.opa_url
        self.gate_llm = LLMClient(s)  # agent `security_gate` (APEX_LLM_KEY)
        self.gate_ = Gate(self.conn, self.store, self.gate_llm, gs)
        self.triage_llm = LLMClient(s.model_copy(update={"agent": "triage", "llm_key": gs.triage_key}))
        self.extractor_llm = LLMClient(s.model_copy(update={"agent": "extractor", "llm_key": ds.extractor_key}))
        self.drafter_llm = LLMClient(s.model_copy(update={"agent": "drafter", "llm_key": ds.drafter_key}))
        self.extractor = Extractor(self.extractor_llm, s.opa_url)
        self._cycle: Any = None
        cp = psycopg.connect(
            load_as(DbSettings).url.get_secret_value(), autocommit=True, prepare_threshold=0, row_factory=dict_row
        )
        self.graph = QuoteGraph(self, cp)

    def llm_spent(self) -> float:
        return sum(c.spent_usd for c in (self.gate_llm, self.triage_llm, self.extractor_llm, self.drafter_llm))

    def cycle_model(self) -> Any:
        if self._cycle is None:
            self._cycle = cycle_time.load(self.store)
        return self._cycle

    def set_status(self, email_id: int, status: str) -> None:
        with db.connect(SCHEMA) as conn, conn.cursor() as cur:
            cur.execute("UPDATE jobs SET status = %s, finished_at = CASE WHEN %s = ANY(%s) THEN now() END WHERE email_id = %s",
                        (status, status, list(FINAL), email_id))  # fmt: skip
            conn.commit()

    # ---- the steps the graph's nodes call (each stores its result, so a node is idempotent)
    def run_gate(self, email_id: int, trace_id: str) -> None:
        """The security gate (S3): stored in gate_results (triage still empty)."""
        g = self.gate_.run(email_id, trace_id)
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO gate_results (email_id, injection_score, hidden_text, sender, reasons, red_banner, needs_human, trace_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (email_id) DO UPDATE SET injection_score = EXCLUDED.injection_score, hidden_text = EXCLUDED.hidden_text,
                     sender = EXCLUDED.sender, reasons = EXCLUDED.reasons, red_banner = EXCLUDED.red_banner,
                     needs_human = EXCLUDED.needs_human, triage = NULL, trace_id = EXCLUDED.trace_id, created_at = now()""",
                (email_id, g.injection["score"], Json(g.hidden), Json(g.sender), Json(g.decision["reasons"]),
                 g.decision["red_banner"], bool(g.decision["needs_human"]), trace_id),
            )  # fmt: skip
        self.conn.commit()
        log("gate", email_id=email_id, red_banner=g.decision["red_banner"], reasons="; ".join(g.decision["reasons"]))

    def run_triage(self, email_id: int, trace_id: str) -> str:
        """Triage (S3) on what the gate passed: the visible text, the checked file types, the sender facts."""
        facts, text, gate_human = self.triage_inputs(email_id)
        t = triage(self.triage_llm, facts, text, trace_id)
        with self.conn.cursor() as cur:
            cur.execute("""UPDATE gate_results SET triage = %s, triage_confidence = %s, triage_reason = %s, language = %s,
                           needs_human = %s WHERE email_id = %s""",
                        (t["category"], t["confidence"], t["reason"], t["language"], bool(gate_human) or t["category"] == "unclear", email_id))  # fmt: skip
        self.conn.commit()
        log("triaged", email_id=email_id, triage=t["category"], confidence=t["confidence"])
        return str(t["category"])

    def triage_inputs(self, email_id: int) -> tuple[dict[str, str], str, bool]:
        """What triage sees (also used by the S10 golden eval, which must call triage exactly like this)."""
        with self.conn.cursor() as cur:
            cur.execute("""SELECT e.raw_sha256, e.in_reply_to IS NOT NULL, e.thread_id IS NOT NULL, g.sender, g.needs_human
                           FROM emails e JOIN gate_results g ON g.email_id = e.id WHERE e.id = %s""", (email_id,))  # fmt: skip
            raw_sha, is_reply, known_thread, sender, gate_human = cur.fetchone()  # type: ignore[misc]
            cur.execute("SELECT filename, true_type, verdict FROM attachments WHERE email_id = %s ORDER BY id", (email_id,))
            files = cur.fetchall()
        msg = email.message_from_bytes(self.store.get(f"raw/{raw_sha}.eml"), policy=policy.default)
        facts = {  # established by our code, so trusted
            "sender is a known customer in Odoo": sender.get("known_name") or "no",
            "is a reply to an earlier email": "yes" if is_reply else "no",
            "the earlier email is in our records": "yes" if known_thread else "no",
            "attachment types (checked)": ", ".join(f"{t} ({v})" for _, t, v in files) or "none",
        }  # fmt: skip
        # the sender writes all of these, so they stay inside the untrusted block
        text = (f"From: {msg.get('From', '')}\nSubject: {msg.get('Subject', '')}\n"
                f"Attachment names: {', '.join(f for f, _, _ in files) or 'none'}\n\n{content.visible_text(msg)}")  # fmt: skip
        return facts, text, bool(gate_human)

    def extract(self, email_id: int, trace_id: str) -> None:
        """S4: only files the gate passed (files/ in MinIO; a ZIP's members, not the ZIP) and the visible text."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT raw_sha256, coalesce(subject, '') FROM emails WHERE id = %s", (email_id,))
            raw_sha, subject = cur.fetchone()  # type: ignore[misc]
            cur.execute("""SELECT filename, true_type, sha256 FROM attachments WHERE email_id = %s AND verdict = 'clean'
                           AND true_type <> 'application/zip' ORDER BY id""", (email_id,))  # fmt: skip
            rows = cur.fetchall()
        msg = email.message_from_bytes(self.store.get(f"raw/{raw_sha}.eml"), policy=policy.default)
        files = [(name, ttype, self.store.get(f"files/{sha}")) for name, ttype, sha in rows]
        x = self.extractor.run(email_id, subject, content.visible_text(msg), files, trace_id)
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO extractions (email_id, result, parts, needs_human, clarification, route, language, llm_calls, trace_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (email_id) DO UPDATE SET result = EXCLUDED.result, parts = EXCLUDED.parts,
                     needs_human = EXCLUDED.needs_human, clarification = EXCLUDED.clarification, route = EXCLUDED.route,
                     language = EXCLUDED.language, llm_calls = EXCLUDED.llm_calls, trace_id = EXCLUDED.trace_id, created_at = now()""",
                (email_id, Json(x.model_dump(mode="json")), len(x.parts), len(x.needs_human), len(x.clarification), x.route,
                 x.requirements.language, x.llm_calls, trace_id),
            )  # fmt: skip
        self.conn.commit()
        log("extracted", email_id=email_id, parts=len(x.parts), needs_human=len(x.needs_human),
            clarification=len(x.clarification), route=x.route, language=x.requirements.language, llm_calls=x.llm_calls)  # fmt: skip

    # ---- the queue
    def run_one(self) -> bool:
        """Take one job and run it through the graph; False when the queue is empty."""
        with self.conn.cursor() as cur:
            cur.execute(RESCUE)
            cur.execute(CLAIM, (HOST,))
            job = cur.fetchone()
        self.conn.commit()
        if not job:
            return False
        job_id, email_id, attempts, decision, followup = job
        for c in (self.gate_llm, self.triage_llm, self.extractor_llm, self.drafter_llm):
            c.email_id = email_id  # S9: every LLM call is tagged and audited with its job
        try:
            if decision and canary.is_canary(self.conn, email_id):  # S9: a synthetic oversight check, never a real job
                status = "done"
                log(
                    "canary decided", email_id=email_id, by=decision.get("by"), result=canary.score(self.conn, email_id, decision)
                )
            elif followup:  # S15: the quote went out N days ago: a reminder for review, unless the customer replied
                status = self.graph.followup(email_id)
                log("follow-up", email_id=email_id, status=status)
            elif decision and self.graph.status(email_id) == "awaiting_review":  # S7: a reviewer decided on the screen
                status = self.graph.resume(email_id, decision)
                log("decision applied", email_id=email_id, action=decision.get("action"), by=decision.get("by"), status=status)
                audit.record("human_decision", str(decision.get("by")), {"event": "decision applied", "action": decision.get("action"),
                             "roles": decision.get("roles"), "reason": decision.get("reason"), "via": decision.get("via"), "status": status},
                             email_id)  # fmt: skip
            else:
                status = self.graph.run(email_id)
                if status == "awaiting_review" and canary.roll(self.opa_url):  # S9: now and then, a known-bad copy
                    log("canary injected", source=email_id, canary=canary.inject(self.conn, email_id))
            error, delay = None, 0
        except PermissionError as e:  # OPA refused the decision here too (policy changed, or OPA down): back to review
            self.conn.rollback()
            log("decision refused", email_id=email_id, error=str(e))
            audit.record("human_decision", str(decision.get("by")), {"event": "decision refused", "action": decision.get("action"),
                         "why": str(e)}, email_id)  # fmt: skip
            with self.conn.cursor() as cur:
                cur.execute("UPDATE jobs SET status = 'awaiting_review', decision = NULL, error = %s, attempts = attempts - 1 WHERE id = %s",
                            (f"decision refused: {e}"[:500], job_id))  # fmt: skip
            self.conn.commit()
            return True
        except (NeedsHuman, NotAllowed, ScannerDown, OSError, psycopg.Error, ValueError, KeyError, audit.AuditUnavailable) as e:
            self.conn.rollback()
            error = f"{type(e).__name__}: {e}"[:500]
            status, delay = ("queued", 30 * attempts) if attempts < MAX_ATTEMPTS else ("failed", 0)
            if isinstance(e, QuotaExhausted) or outage(e):  # not the job's fault: wait for it, don't burn attempts (S19)
                status, delay = "queued", quota_wait(e) if isinstance(e, QuotaExhausted) else OUTAGE_WAIT_S
                with self.conn.cursor() as cur:
                    cur.execute("UPDATE jobs SET attempts = attempts - 1 WHERE id = %s", (job_id,))
            log("job error", job_id=job_id, email_id=email_id, attempt=attempts, next=status, error=error)
        with self.conn.cursor() as cur:
            cur.execute("""UPDATE jobs SET status = %s, error = %s, available_at = now() + make_interval(secs => %s),
                           finished_at = CASE WHEN %s = ANY(%s) THEN now() END WHERE id = %s""",
                        (status, error, delay, status, list(FINAL), job_id))  # fmt: skip
            if decision and error is None:  # applied; a failed attempt keeps it, so the retry resumes with it
                cur.execute("UPDATE jobs SET decision = NULL WHERE id = %s", (job_id,))
        self.conn.commit()
        return True

    def rescore(self) -> int:
        """S11: queue priority for jobs that reached review before S11, from what is stored (no LLM call)."""
        with self.conn.cursor() as cur:
            cur.execute("""SELECT r.email_id, r.value_usd, r.route, x.result->'requirements',
                                  (SELECT coalesce(jsonb_agg(e.result), '[]'::jsonb) FROM estimates e WHERE e.email_id = r.email_id)
                           FROM review_items r JOIN jobs j USING (email_id) LEFT JOIN extractions x USING (email_id)
                           WHERE j.status = 'awaiting_review' AND r.payload->'risk'->'priority' IS NULL""")  # fmt: skip
            rows = cur.fetchall()
            for e, value, route, req, est in rows:
                infeasible = any("is not feasible" in f for x in est for f in x.get("flags", []))
                prio = priority.score(self.conn, est, float(value or 0), req or {}, infeasible, route == "no_bid")
                cur.execute(
                    "UPDATE review_items SET payload = jsonb_set(payload, '{risk,priority}', %s::jsonb) WHERE email_id = %s",
                    (Json(prio), e),
                )
        self.conn.commit()
        return len(rows)

    def backfill_review_items(self) -> int:
        """Jobs that paused at review before the review screen existed (S6) get their review_items row from the checkpoint."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT email_id FROM jobs WHERE status = 'awaiting_review' AND email_id NOT IN (SELECT email_id FROM review_items)"
            )
            ids = [r[0] for r in cur.fetchall()]
            for e in ids:
                snap = self.graph.graph.get_state(self.graph.config(e))
                if not snap.interrupts:
                    continue
                p = snap.interrupts[0].value
                cur.execute("INSERT INTO review_items (email_id, route, value_usd, needs_senior, payload) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (e, p["route"], p["risk"].get("value_usd", 0.0), bool(p["risk"].get("needs_senior")), Json(p)))  # fmt: skip
        self.conn.commit()
        return len(ids)

    def forget(self, where: str) -> int:
        """Drop the graph threads (checkpoints) of the jobs matching `where`, so they run again from the start."""
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT email_id FROM jobs WHERE {where}")  # noqa: S608  # nosec B608 (fixed strings from main())
            ids = [r[0] for r in cur.fetchall()]
            for i in ids:
                self.graph.saver.delete_thread(f"email-{i}")
            cur.execute(
                f"UPDATE jobs SET status = 'queued', attempts = 0, available_at = now(), error = NULL, decision = NULL WHERE {where}"  # nosec B608 (fixed strings from main())
            )  # noqa: S608
        self.conn.commit()
        return len(ids)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drain", action="store_true")
    ap.add_argument("--redo", action="store_true", help="every job again from the security gate")
    ap.add_argument("--redo-extraction", action="store_true", help="RFQs again from extraction (gate + triage kept)")
    ap.add_argument("--retry-failed", action="store_true", help="failed jobs back into the queue (e.g. after an outage)")
    ap.add_argument("--redo-estimates", action="store_true", help="RFQs again from the estimate (no LLM until the draft)")
    ap.add_argument(
        "--rescore",
        action="store_true",
        help="S11: give every waiting job without one its queue priority, from stored estimates (no LLM)",
    )
    ap.add_argument(
        "--rerun",
        type=int,
        action="append",
        default=[],
        metavar="EMAIL_ID",
        help="ONE job again from the estimate (stored gate/triage/extraction reused: only the draft calls the LLM)",
    )
    ap.add_argument(
        "--followup-now",
        type=int,
        action="append",
        default=[],
        metavar="EMAIL_ID",
        help="S15: make a sent quote's follow-up due now (class demo / test; policy says after N days)",
    )
    ap.add_argument(
        "--to-people",
        type=int,
        action="append",
        default=[],
        metavar="EMAIL_ID",
        help="S19 runbook: hand waiting jobs to people (status manual), e.g. a backlog during an AI outage",
    )
    ap.add_argument("--to-people-since", metavar="ISO_TIME", help="S19: every queued job that arrived since then")
    ap.add_argument("--why", default="AI outage: quoted by hand", help="the reason shown with --to-people")
    ap.add_argument("--expire", action="store_true", help="S15: tag open Odoo quotations past their validity 'Expired'")
    ap.add_argument(
        "--supplier-rfqs",
        type=int,
        action="append",
        default=[],
        metavar="EMAIL_ID",
        help="S17: draft a costed job's supplier RFQs from its stored estimate (no LLM)",
    )
    a = ap.parse_args()
    w = Worker()
    if a.to_people or a.to_people_since:
        with w.conn.cursor() as cur:
            cur.execute("""UPDATE jobs j SET status = 'manual', error = %s, finished_at = now() FROM emails e
                           WHERE e.id = j.email_id AND j.status IN ('queued', 'failed')
                             AND (j.email_id = ANY(%s) OR (%s::timestamptz IS NOT NULL AND e.ingested_at >= %s::timestamptz))
                           RETURNING j.email_id""", (f"handed to people: {a.why}", a.to_people, a.to_people_since, a.to_people_since))  # fmt: skip
            handed = [r[0] for r in cur.fetchall()]
        w.conn.commit()
        audit.record("admin", "operator", {"event": "jobs handed to people", "why": a.why, "jobs": len(handed)}, None)
        print(json.dumps({"handed_to_people": len(handed), "why": a.why}))
        return
    if a.supplier_rfqs:
        from apex_agent import suppliers
        from apex_agent.costing.quote import fetch_facts

        out = {}
        for e in a.supplier_rfqs:
            with w.conn.cursor() as cur:
                cur.execute("SELECT result FROM estimates WHERE email_id = %s", (e,))
                est = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT result FROM extractions WHERE email_id = %s", (e,))
                ext: dict[str, Any] = (cur.fetchone() or [{}])[0]
                cur.execute(
                    "SELECT g.sender->>'known_customer', m.from_addr, m.from_name FROM emails m JOIN gate_results g ON g.email_id = m.id WHERE m.id = %s",
                    (e,),
                )
                cust, addr, name = cur.fetchone()  # type: ignore[misc]
            facts = fetch_facts(
                w.opa_url,
                sorted({x["part"]["material"] for x in est}),
                sorted({o for x in est for o in x["part"]["outside"]}),
                cust,
            )
            out[e] = suppliers.draft_all(w.conn, w.store, w.opa_url, e, est, facts, ext, {"from_addr": addr, "from_name": name})
        print(json.dumps(out))
        return
    if a.followup_now:
        with w.conn.cursor() as cur:
            cur.execute("UPDATE jobs SET followup_at = now() WHERE status = 'sent' AND email_id = ANY(%s) RETURNING email_id",
                        (a.followup_now,))  # fmt: skip
            print(json.dumps({"followup_due_now": [r[0] for r in cur.fetchall()]}))
        w.conn.commit()
        return
    if a.expire:
        print(json.dumps(reply_tool("erp-mcp-write", "mark_expired", {}, "expiry")))
        return
    rfq = "email_id IN (SELECT email_id FROM gate_results WHERE triage = 'rfq')"
    with w.conn.cursor() as cur:
        if a.redo:
            cur.execute("TRUNCATE gate_results, extractions, estimates, decisions, feedback, review_items")
        elif a.redo_extraction:
            cur.execute("TRUNCATE extractions, estimates")
        elif a.redo_estimates:
            cur.execute("TRUNCATE estimates")
    w.conn.commit()
    if a.rescore:
        print(json.dumps({"rescored": w.rescore()}))
        return
    if a.rerun:  # S11: a targeted re-run, so a test or a class demo spends 1-2 LLM calls, not one per RFQ
        ids = ",".join(str(int(i)) for i in a.rerun)
        with w.conn.cursor() as cur:
            cur.execute(f"DELETE FROM estimates WHERE email_id IN ({ids})")  # nosec B608 (integers)
            cur.execute(f"DELETE FROM review_items WHERE email_id IN ({ids})")  # nosec B608 (integers)
        w.conn.commit()
        w.forget(f"email_id IN ({ids})")
    admin = a.retry_failed or a.redo or a.redo_extraction or a.redo_estimates or bool(a.rerun)
    if a.redo:
        w.forget("TRUE")
    elif a.redo_extraction or a.redo_estimates:
        w.forget(rfq)
    if a.retry_failed:
        w.forget("status = 'failed'")
    if admin and not a.drain:  # an admin action: requeue, then exit (the worker service does the work)
        with w.conn.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM jobs GROUP BY status ORDER BY 1")
            print(json.dumps({"requeued": True, "jobs": dict(cur.fetchall()), "next": "the worker service processes them"}))
        return
    if a.drain:
        # Wait for the queue to empty, not just for "nothing claimable right now": a job waiting out an outage or the
        # quota (available_at in the future) is still pending, and an evaluation run straight after must see it done.
        # ponytail: a fixed 60-minute ceiling (a spent daily quota waits 30 min per round); then report what is left.
        n, deadline = 0, time.time() + 3600
        while True:
            if w.run_one():
                n += 1
                continue
            with w.conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM jobs WHERE status IN ('queued', 'running', 'decided')")
                pending = cur.fetchone()[0]  # type: ignore[index]
            w.conn.commit()
            if not pending or time.time() > deadline:
                break
            time.sleep(5)
        with w.conn.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM jobs GROUP BY status ORDER BY 1")
            print(json.dumps({"processed": n, "jobs": dict(cur.fetchall())}))
        return
    with w.conn.cursor() as cur:
        cur.execute(RESCUE_MINE, (HOST,))
        rescued = cur.rowcount
    w.conn.commit()
    log("worker started", host=HOST, resumed_jobs=rescued, review_items_backfilled=w.backfill_review_items())
    next_outcomes = next_expiry = 0.0
    while True:
        if not w.run_one():
            if time.time() >= next_outcomes:  # S8 learn: quotations confirmed or cancelled in Odoo since the last look
                try:
                    if (res := outcomes.sync(w.conn)).get("changed") or res.get("error"):
                        log("outcomes", **res)
                    if (st := supplier_tick(w.conn, w.store))["sent"] or st["timed_out"]:  # S17: approved sends, timeouts
                        log("supplier rfqs", **st)
                    if time.time() >= next_expiry:  # S15: daily, open quotations past their validity get "Expired"
                        log("expired quotations", **reply_tool("erp-mcp-write", "mark_expired", {}, "expiry"))
                        next_expiry = time.time() + 86400
                    costs.check(w.conn, {"security_gate": w.gate_llm, "triage": w.triage_llm, "extractor": w.extractor_llm,
                                         "drafter": w.drafter_llm})  # S9: budget alerts at 50/80/100 %  # fmt: skip
                except (
                    OSError,
                    RuntimeError,
                    psycopg.Error,
                    httpx.HTTPError,
                    audit.AuditUnavailable,
                ) as e:  # never stop the worker loop
                    w.conn.rollback()
                    log("outcomes error", error=str(e)[:300])
                next_outcomes = time.time() + OUTCOMES_EVERY_S
            time.sleep(2)


if __name__ == "__main__":
    main()
