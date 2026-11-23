"""The security gate for one ingested email (S3). Order: files -> text -> Prompt Guard -> sender -> OPA.

It never stops a job: a flagged email still goes on, with a red banner and `needs_human` (the decision and
its reasons come from OPA, apex/gate/decision). Only files are stopped: a quarantined file is never parsed.
"""

import email
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from typing import Any

import psycopg
from psycopg.types.json import Json

from apex_agent import opa
from apex_agent.ingest.store import Store, sha256
from apex_agent.llm.client import AiDisabled, LLMClient
from apex_agent.security import content, files, sender
from apex_agent.security.pii import log, redact
from apex_agent.settings import GateSettings

CHUNK = 1800  # characters per Prompt Guard call (the model reads at most 512 tokens, ~2,000 characters)
MAX_CHUNKS = 20  # ponytail: per email; a 200-page PDF would only be scanned in part. Upgrade: sample all pages.
NO_ZIP = {"max_files": 0, "max_total_mb": 0, "max_ratio": 0}  # OPA unreachable: every ZIP fails its limits
# OPA unreachable (e.g. restarted by `make policy` / `make demo-agent`): fail closed by WAITING, not by a permanent verdict.
# Nothing passes the gate meanwhile; the worker re-queues the job without using its attempts (S19 outage) and decides again.
OPA_DOWN = "OPA unreachable: the security gate's decision waits (fail closed: nothing passes meanwhile)"


@dataclass
class GateResult:
    email: EmailMessage
    body: str  # visible text only: what goes on to triage (and later extraction)
    hidden: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    injection: dict[str, Any] = field(default_factory=dict)
    sender: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] = field(default_factory=dict)


class Gate:
    def __init__(self, conn: psycopg.Connection[Any], store: Store, guard_llm: LLMClient, gs: GateSettings) -> None:
        self.conn, self.store, self.llm, self.gs = conn, store, guard_llm, gs
        self.opa_url, self.tls = guard_llm.s.opa_url, guard_llm.s.tls()

    # ---- files
    def _check(self, email_id: int, att_id: int, filename: str, digest: str, depth: int) -> list[tuple[str, bytes]]:
        """Decide one stored file; returns the readable files it yields (itself, or a safe ZIP's members).
        Always decides afresh (idempotent), so a retried job or `worker --redo` after a policy change is correct."""
        data = self.store.attachment(digest)
        limits = opa.decide(self.opa_url, "policy/gate/files/zip", {}, self.tls) or NO_ZIP  # the limits OPA holds
        f = files.facts(filename, data, (self.gs.clamd_host, self.gs.clamd_port), limits)
        reasons = opa.decide(self.opa_url, "apex/gate/file_reasons", f.opa_input(depth), self.tls)
        if reasons is None:
            raise AiDisabled(OPA_DOWN)
        reasons = sorted(reasons)
        verdict = "quarantined" if reasons else "clean"
        self.store.place(digest, "quarantine" if reasons else "files")
        with self.conn.cursor() as cur:
            cur.execute("UPDATE attachments SET true_type=%s, verdict=%s, reasons=%s::jsonb WHERE id=%s",
                        (f.true_type, verdict, Json(reasons), att_id))  # fmt: skip
        self.conn.commit()
        log("file checked", email_id=email_id, file=filename, true_type=f.true_type, verdict=verdict, reasons=reasons)
        if reasons:
            return []
        out = [] if f.true_type == "application/zip" else [(filename, data)]
        for name, member in f.members:  # a safe ZIP: every file inside goes through the same checks
            h = sha256(member)
            self.store.put(f"incoming/{h}", member)
            with self.conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO attachments (email_id, filename, sha256, size, parent_id) VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (email_id, filename, sha256, parent_id) DO UPDATE SET size = EXCLUDED.size RETURNING id""",
                    (email_id, name, h, len(member), att_id),
                )
                child = int(cur.fetchone()[0])  # type: ignore[index]
            self.conn.commit()
            out += self._check(email_id, child, name, h, depth + 1)
        return out

    # ---- Prompt Guard
    def _guard(self, pieces: list[tuple[str, str]], trace_id: str) -> dict[str, Any]:
        best: dict[str, Any] = {"score": 0.0, "where": "nothing scanned", "calls": 0}
        chunks = [(where, t[i : i + CHUNK]) for where, t in pieces if t.strip() for i in range(0, len(t), CHUNK)]
        for where, chunk in chunks[:MAX_CHUNKS]:
            c = self.llm.chat("quote-guard", [{"role": "user", "content": chunk}], max_tokens=8, cache=True, trace_id=trace_id,
                              tags=["s3:guard"], redacted=redact(chunk))  # fmt: skip
            score = float(c.text.strip())
            best["calls"] += 1
            if score > best["score"]:
                best.update(score=round(score, 4), where=where)
        best["chunks_skipped"] = max(0, len(chunks) - MAX_CHUNKS)
        return best

    def run(self, email_id: int, trace_id: str) -> GateResult:
        with self.conn.cursor() as cur:
            cur.execute("SELECT raw_sha256 FROM emails WHERE id = %s", (email_id,))
            raw = self.store.get(f"raw/{cur.fetchone()[0]}.eml")  # type: ignore[index]
            cur.execute("SELECT id, filename, sha256 FROM attachments WHERE email_id = %s AND parent_id IS NULL ORDER BY id",
                        (email_id,))  # fmt: skip
            atts = cur.fetchall()
        msg = email.message_from_bytes(raw, policy=policy.default)

        readable: list[tuple[str, bytes]] = []
        for att_id, filename, digest in atts:
            readable += self._check(email_id, att_id, filename, digest, 0)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT filename, true_type, verdict, reasons FROM attachments WHERE email_id = %s ORDER BY id", (email_id,)
            )
            file_rows = [{"filename": r[0], "true_type": r[1], "verdict": r[2], "reasons": r[3]} for r in cur.fetchall()]

        body, h = content.email_texts(msg)
        visible = content.visible_text(msg)
        hidden = [{"where": "hidden HTML text", "text": t} for t in h.hidden]
        # Hidden text first and joined per source: a fragmented instruction is read whole, and the chunk cap
        # can never skip it.
        pieces = [("hidden HTML text", "\n".join(h.hidden)), ("the email body", visible)]
        for name, data in readable:
            if files.true_type(data) == "application/pdf":
                p = content.pdf_text(data)
                pieces.insert(0, (f"hidden text in {name}", "\n".join(p.hidden)))
                pieces.append((f"text of {name}", p.visible))
                hidden += [{"where": f"hidden text in {name}", "text": t} for t in p.hidden]
            elif name.lower().endswith(".dxf"):  # S9 (red team): a DXF's TEXT/MTEXT notes are text an attacker controls too
                pieces.append((f"text of {name}", content.dxf_text(data)))
        zero_width = body.zero_width + h.zero_width
        if zero_width:
            hidden.append({"where": "zero-width characters", "text": f"{zero_width} removed"})

        injection = self._guard(pieces, trace_id)
        who = {
            **sender.authenticate(raw, str(msg.get("From", "")), self.gs.lab_dns),
            **sender.customer_check(str(msg.get("From", ""))),
        }
        opa_in = {"injection": injection, "files": file_rows, "sender": who}
        decision = opa.decide(self.opa_url, "apex/gate/decision", opa_in, self.tls)
        if not decision:
            raise AiDisabled(OPA_DOWN)
        return GateResult(msg, visible, [{**x, "text": redact(x["text"])[:300]} for x in hidden], file_rows,
                          injection, who, decision)  # fmt: skip
