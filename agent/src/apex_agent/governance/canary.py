"""Canaries (S9 human oversight): known-bad drafts, injected at random, that detect rubber-stamping.

A canary is a SYNTHETIC copy of a real job that just reached review: same email, drawings, extraction and costs, but
the draft's first unit price is 15 % too high in the customer's text (the breakdown beside it is right). A careful
reviewer notices and edits or rejects it; a rubber stamp approves it. The copy is a new email/job row, so a real
customer's RFQ is never touched, and the worker scores a decided canary instead of resuming any graph: nothing is
ever sent. The `canaries` table is not readable by the review screen's role, so the screen can't tell.

Usage: python -m apex_agent.governance.canary inject <email_id>    # copy that job as a canary now (class demo)
       python -m apex_agent.governance.canary report               # caught / missed per reviewer
"""

import json
import re
import secrets
import sys
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Json

from apex_agent import audit, opa
from apex_agent.knowledge import db


def roll(opa_url: str) -> bool:
    """Inject a canary now? policy.yaml -> oversight.canary_rate (OPA data); no answer = no canary."""
    cfg = opa.decide(opa_url, "policy/oversight", {}) or {}
    rate = float(cfg.get("canary_rate") or 0.0)
    return rate > 0 and secrets.randbelow(1_000_000) < rate * 1_000_000


def wrong_text(text: str, price: float) -> tuple[str, str, str] | None:
    """The draft with its first mention of `price` raised by 15 % (in the same format: 73.84 or 73,84)."""
    wrong = f"{round(price * 1.15, 2):.2f}"
    short = f"{price:.2f}".rstrip("0").rstrip(".")  # the drafter may write 778.3 for 778.30
    forms = [(f"{price:.2f}", wrong), (short, wrong)]
    for right, bad in [*forms, *[(r.replace(".", ","), b.replace(".", ",")) for r, b in forms]]:
        if re.search(rf"(?<![\d.,]){re.escape(right)}(?![\d])", text):
            return re.sub(rf"(?<![\d.,]){re.escape(right)}(?![\d])", bad, text, count=1), bad, right
    return None


def inject(conn: psycopg.Connection[Any], source: int) -> int | None:
    """Copy job `source` (waiting for review, with a quote draft) as a canary; returns the canary's email id."""
    with conn.cursor() as cur:
        cur.execute("SELECT payload, route, value_usd, needs_senior FROM review_items WHERE email_id = %s", (source,))
        row = cur.fetchone()
        cur.execute("SELECT result FROM estimates WHERE email_id = %s ORDER BY part_code LIMIT 1", (source,))
        est = cur.fetchone()
        if not row or not est or (row[0].get("draft") or {}).get("mode") != "quote":
            return None
        payload, route, value, senior = row
        changed = wrong_text(payload["draft"].get("customer_language_text") or "", est[0]["breaks"][0]["unit_price"])
        if not changed:
            return None
        text, bad, right = changed
        payload = {**payload, "draft": {**payload["draft"], "customer_language_text": text}}
        cur.execute("""INSERT INTO emails (message_id, raw_sha256, mailbox, imap_uid, from_addr, from_name, subject, sent_at)
                       SELECT %s, raw_sha256, 'canary', NULL, from_addr, from_name, subject, sent_at FROM emails WHERE id = %s
                       RETURNING id""", (f"<canary-{uuid.uuid4().hex}@apex.local>", source))  # fmt: skip
        new = cur.fetchone()[0]  # type: ignore[index]
        cur.execute("""INSERT INTO attachments (email_id, filename, claimed_type, sha256, size, true_type, verdict, reasons)
                       SELECT %s, filename, claimed_type, sha256, size, true_type, verdict, reasons FROM attachments
                       WHERE email_id = %s AND parent_id IS NULL""", (new, source))  # fmt: skip
        for table in ("extractions", "estimates"):  # every column, only the email id swapped (whatever later phases add)
            cur.execute(f"""INSERT INTO {table} SELECT (jsonb_populate_record(NULL::{table}, to_jsonb(x) || jsonb_build_object('email_id', %s))).*
                            FROM {table} x WHERE email_id = %s""", (new, source))  # nosec B608 (a fixed table name)  # fmt: skip
        cur.execute("INSERT INTO review_items (email_id, route, value_usd, needs_senior, payload) VALUES (%s,%s,%s,%s,%s)",
                    (new, route, value, senior, Json({**payload, "email_id": new})))  # fmt: skip
        cur.execute(
            "INSERT INTO canaries (email_id, source_id, wrong, right_price) VALUES (%s,%s,%s,%s)", (new, source, bad, right)
        )
        cur.execute("INSERT INTO jobs (email_id, status, attempts, finished_at) VALUES (%s, 'awaiting_review', 0, now())", (new,))
    conn.commit()
    audit.record("admin", "oversight", {"event": "canary injected", "source": source, "canary": new}, new)
    return int(new)


def is_canary(conn: psycopg.Connection[Any], email_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM canaries WHERE email_id = %s", (email_id,))
        return cur.fetchone() is not None


def score(conn: psycopg.Connection[Any], email_id: int, decision: dict[str, Any]) -> str:
    """A decided canary: caught (rejected, re-costed, clarified, or the wrong price edited away) or missed."""
    with conn.cursor() as cur:
        cur.execute("SELECT wrong FROM canaries WHERE email_id = %s", (email_id,))
        wrong = cur.fetchone()[0]  # type: ignore[index]
        text = (decision.get("edits") or {}).get("customer_language_text") or ""
        missed = decision["action"] == "approve" or (decision["action"] == "edit" and wrong in text)
        result = "missed" if missed else "caught"
        cur.execute(
            "UPDATE canaries SET result = %s, decided_by = %s WHERE email_id = %s", (result, decision.get("by"), email_id)
        )
        cur.execute("""INSERT INTO decisions (email_id, action, decided_by, reason, final_text) VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (email_id) DO NOTHING""",
                    (email_id, decision["action"], decision.get("by"), f"[oversight canary: {result}] {decision.get('reason') or ''}", text or None))  # fmt: skip
    conn.commit()
    audit.record("human_decision", str(decision.get("by")), {"event": f"canary {result}", "action": decision["action"]}, email_id)
    return result


def report(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("""SELECT decided_by, count(*) FILTER (WHERE result = 'caught'), count(*) FILTER (WHERE result = 'missed')
                       FROM canaries WHERE result IS NOT NULL GROUP BY 1 ORDER BY 1""")
        rows = cur.fetchall()
        cur.execute("SELECT count(*) FROM canaries WHERE result IS NULL")
        pending = cur.fetchone()[0]  # type: ignore[index]
    return {"by_reviewer": {r[0]: {"caught": r[1], "missed": r[2]} for r in rows}, "waiting": pending}


if __name__ == "__main__":
    c = db.connect()
    if sys.argv[1:2] == ["inject"] and len(sys.argv) == 3:
        print(json.dumps({"canary": inject(c, int(sys.argv[2]))}))
    elif sys.argv[1:] == ["report"]:
        print(json.dumps(report(c)))
    else:
        raise SystemExit(__doc__)
