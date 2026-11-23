"""The weekly digest for management (S15, W17: no management view). The NUMBERS come from SQL; the LLM only writes a
short narrative around them, and S6's faithfulness check rejects any number that isn't one of the KPIs (one corrective
retry; a digest that still fails is stored as unfaithful and shown with a warning, never silently).

Usage (worker): python -m apex_agent.governance.digest     # this week's digest -> the `digests` table (Digest page)
"""

import json
from datetime import date, timedelta
from statistics import median
from typing import Any

from apex_agent.graph.draft import allowed_numbers, unfaithful

SYSTEM = """You write Apex Precision Components' weekly quoting summary for the managing director (Meera).
Use ONLY the numbers in the KPIs JSON, exactly as given: do not calculate, round, convert or add any number (no
percentages that aren't given, no totals). 5-8 short sentences, plain text: what came in, what went out, how fast,
what customers answered and why they said no, and what needs attention. No greeting, no sign-off."""


def kpis(conn: Any, week: date) -> dict[str, Any]:
    start, end = week, week + timedelta(days=7)
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM emails e JOIN gate_results g ON g.email_id = e.id
                       WHERE g.triage = 'rfq' AND e.mailbox <> 'canary' AND e.ingested_at >= %s AND e.ingested_at < %s""", (start, end))  # fmt: skip
        rfqs = cur.fetchone()[0]
        cur.execute("""SELECT extract(epoch FROM a.at - e.ingested_at) / 3600 FROM actions a JOIN emails e ON e.id = a.email_id
                       WHERE a.result ? 'quotation' AND a.at >= %s AND a.at < %s""", (start, end))  # fmt: skip
        hours = [float(h) for (h,) in cur.fetchall()]
        cur.execute(
            "SELECT count(*) FROM jobs j JOIN emails e ON e.id = j.email_id WHERE j.status = 'awaiting_review' AND e.mailbox <> 'canary'"
        )
        waiting = cur.fetchone()[0]
        cur.execute(
            "SELECT result->>'route', count(*) FROM replies WHERE created_at >= %s AND created_at < %s GROUP BY 1 ORDER BY 1",
            (start, end),
        )
        routes = dict(cur.fetchall())
        cur.execute("""SELECT result->>'detail' FROM replies WHERE result->>'route' = 'reject' AND created_at >= %s AND created_at < %s
                       AND coalesce(result->>'detail', '') <> ''""", (start, end))  # fmt: skip
        lost = [r for (r,) in cur.fetchall()]
        cur.execute("SELECT outcome, count(*) FROM outcomes GROUP BY 1")
        outcomes = dict(cur.fetchall())
        cur.execute("SELECT coalesce(sum(spent_usd), 0) FROM graph_events WHERE at >= %s AND at < %s", (start, end))
        ai = float(cur.fetchone()[0])
        cur.execute("SELECT data FROM capacity_snapshots ORDER BY id DESC LIMIT 1")
        snap = (cur.fetchone() or [None])[0]
        cur.execute("SELECT count(*) FROM jobs WHERE followup_at IS NOT NULL")
        followups = cur.fetchone()[0]
    over = sorted({wc for wc, cells in (snap or {}).get("rows", {}).items() for c in cells if (c.get("pct") or 0) > 100})
    return {"week_starting": week.isoformat(), "rfqs_received": rfqs, "quotes_sent": len(hours),
            "median_hours_rfq_to_quote": round(median(hours), 1) if hours else None, "waiting_for_review": waiting,
            "customer_replies_by_route": routes, "lost_reasons": lost, "quotations_won_all_time": outcomes.get("won", 0),
            "quotations_lost_all_time": outcomes.get("lost", 0), "ai_cost_usd": round(ai, 2), "follow_ups_scheduled": followups,
            "work_centers_over_capacity_with_expected_wins": over}  # fmt: skip


def write(llm: Any, k: dict[str, Any], trace_id: str | None = None) -> dict[str, Any]:
    nums, dates = allowed_numbers(k, "")
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "KPIs:\n" + json.dumps(k, indent=1)},
    ]
    text, bad = "", []
    for _ in range(2):
        text = llm.chat(
            "quote-reasoner", msgs, max_tokens=1200, trace_id=trace_id, tags=["s15:digest"], reasoning_effort="low"
        ).text.strip()
        bad = sorted(set(unfaithful(text, nums, dates)))
        if not bad:
            break
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content": f"These numbers are not in the KPIs: {bad}. Rewrite using only the given numbers."}]  # fmt: skip
    return {"narrative": text, "faithful": not bad, "unfaithful": bad}


def run(w: Any, week: date | None = None) -> dict[str, Any]:
    week = week or (date.today() - timedelta(days=date.today().weekday()))
    k = kpis(w.conn, week)
    d = write(w.drafter_llm, k)
    with w.conn.cursor() as cur:
        cur.execute("INSERT INTO digests (week, kpis, narrative, faithful, unfaithful) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                    (week, json.dumps(k), d["narrative"], d["faithful"], json.dumps(d["unfaithful"])))  # fmt: skip
        did = cur.fetchone()[0]
    w.conn.commit()
    return {"id": did, "week": week.isoformat(), **d, "kpis": k}


if __name__ == "__main__":
    from apex_agent.worker import Worker

    print(json.dumps(run(Worker()), default=str))
