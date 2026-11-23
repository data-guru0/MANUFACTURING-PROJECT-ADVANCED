"""Portfolio capacity (S14, W7: promising the same machine time twice). Deterministic: no LLM.

Expected load per work center per week = the confirmed work (Odoo's open work orders, forward-loaded on each work
center's shift calendar from today) + every OPEN quote's planned minutes x its P(win), in the week its step is planned.
Open quotes = the agent's estimates of jobs waiting for review or sent and not yet answered.
# ponytail: Odoo's own draft/sent quotations (legacy, no planned minutes) are not counted; estimate them with the
# engine if the legacy channel stays open.

    weekly()      the heatmap rows: capacity, confirmed and expected minutes and the % used, per work center and week
    expected()    expected minutes per work center (what new quotes' delivery dates load on top of the backlog)
    overcommit()  the weeks a quote's own steps fall in where confirmed + expected > capacity: a risk flag

Usage (worker): python -m apex_agent.costing.capacity [--flood N]   # store a snapshot; --flood: what N more open
                                                                    # quotes like the biggest one would do to a date
"""

import json
import sys
from datetime import date, timedelta
from typing import Any

from apex_agent.costing.leadtime import Calendar

BASELINE_P_WIN = 0.262  # L6 baseline; used for a quote with no P(win) of its own
OPEN_QUOTES_SQL = """
    -- one potential order per customer and part (the latest RFQ: a re-sent RFQ is not a second order); canaries (S9's
    -- synthetic oversight checks) are not quotes; P(win): the win model's only when validated, else the queue's
    SELECT DISTINCT ON (coalesce(g.sender->>'known_customer', m.from_addr), e.part_code)
           e.email_id, e.part_code, e.result->'delivery'->'steps',
           CASE WHEN (e.result->'win'->>'validated')::boolean THEN (e.result->'win'->>'p_win')::float
                ELSE (r.payload->'risk'->'priority'->>'p_win')::float END
    FROM estimates e JOIN jobs j USING (email_id) JOIN emails m ON m.id = e.email_id
         LEFT JOIN gate_results g USING (email_id) LEFT JOIN review_items r USING (email_id)
    WHERE j.status IN ('awaiting_review', 'sent') AND m.mailbox <> 'canary' AND e.email_id <> %s
    ORDER BY coalesce(g.sender->>'known_customer', m.from_addr), e.part_code, e.email_id DESC"""


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def open_quotes(conn: Any, exclude_email: int = -1) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(OPEN_QUOTES_SQL, (exclude_email,))
        rows = cur.fetchall()
    return [
        {"email_id": e, "part": c, "steps": steps or [], "p_win": float(p) if p is not None else BASELINE_P_WIN}
        for e, c, steps, p in rows
    ]


def expected(quotes: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for q in quotes:
        for st in q["steps"]:
            out[st["wc"]] = out.get(st["wc"], 0.0) + float(st["minutes"]) * q["p_win"]
    return {wc: round(m, 1) for wc, m in out.items()}


def weekly(workload: dict[str, Any], quotes: list[dict[str, Any]], today: date, weeks: int = 12) -> dict[str, Any]:
    hours = workload["hours_by_weekday"]
    cal = Calendar(hours)
    for wc, minutes in workload["open_minutes"].items():  # confirmed work first, from today, as leadtime does
        if wc in cal.cap and minutes > 0:
            cal.load(wc, today, minutes)
    start = monday(today)
    week_list = [start + timedelta(weeks=i) for i in range(weeks)]
    rows: dict[str, list[dict[str, Any]]] = {}
    for wc in sorted(hours):
        cap = {w: sum(cal.cap[wc].get((w + timedelta(days=d)).weekday(), 0.0) for d in range(7) if w + timedelta(days=d) >= today)
               for w in week_list}  # fmt: skip
        conf = {w: 0.0 for w in week_list}
        for (cw, day), used in cal.used.items():
            if cw == wc and monday(day) in conf:
                conf[monday(day)] += used
        rows[wc] = [
            {"week": w.isoformat(), "capacity": round(cap[w]), "confirmed": round(conf[w]), "expected": 0.0} for w in week_list
        ]
    idx = {w.isoformat(): i for i, w in enumerate(week_list)}
    for q in quotes:
        for st in q["steps"]:
            w = max(monday(date.fromisoformat(st["from"])), start).isoformat()
            if st["wc"] in rows and w in idx:
                rows[st["wc"]][idx[w]]["expected"] += float(st["minutes"]) * q["p_win"]
    for cells in rows.values():
        for c in cells:
            c["expected"] = round(c["expected"])
            c["pct"] = round(100 * (c["confirmed"] + c["expected"]) / c["capacity"]) if c["capacity"] else None
    return {"today": today.isoformat(), "weeks": [w.isoformat() for w in week_list], "rows": rows, "open_quotes": len(quotes)}


def overcommit(snapshot: dict[str, Any], steps: list[dict[str, Any]]) -> list[str]:
    """The weeks this quote's own steps run in, on its work centers, where confirmed + expected already exceed capacity."""
    flags = []
    for st in steps:
        first, last = monday(date.fromisoformat(st["from"])), monday(date.fromisoformat(st["to"]))
        for c in snapshot["rows"].get(st["wc"], []):
            w = date.fromisoformat(c["week"])
            if first <= w <= last and c["pct"] is not None and c["pct"] > 100:
                flags.append(f"over-commitment risk: {st['wc']} week of {c['week']} is at {c['pct']} % "
                             f"(confirmed {c['confirmed']} + expected wins {c['expected']} of {c['capacity']} min)")  # fmt: skip
    return flags


def store_snapshot(conn: Any, snap: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO capacity_snapshots (data) VALUES (%s)", (json.dumps(snap),))
    conn.commit()


if __name__ == "__main__":
    import os

    from apex_agent.costing import leadtime
    from apex_agent.costing.quote import fetch_facts
    from apex_agent.knowledge import db

    args = sys.argv[1:]
    flood = int(args[args.index("--flood") + 1]) if "--flood" in args else 0
    conn = db.connect()
    facts = fetch_facts(os.environ.get("APEX_OPA_URL", "http://opa:8181"), [], [], None)
    load = facts["load"]
    quotes = open_quotes(conn)
    snap = weekly(load, quotes, date.today())
    store_snapshot(conn, snap)
    out: dict[str, Any] = {"open_quotes": len(quotes), "expected_minutes": expected(quotes),
                           "busiest": sorted(((wc, max(c["pct"] or 0 for c in cells)) for wc, cells in snap["rows"].items()),
                                             key=lambda x: -x[1])[:4]}  # fmt: skip
    if flood:  # what-if, in memory only: N more open quotes like the biggest one; nothing is stored or sent
        big = max(quotes, key=lambda q: sum(float(s["minutes"]) for s in q["steps"]))
        route = [s["wc"] for s in big["steps"]]
        per_part = {s["wc"]: (float(s["minutes"]),) * 3 for s in big["steps"]}

        def committed(extra: list[dict[str, Any]]) -> str:
            exp = expected(quotes + extra)
            wl = {
                **load,
                "open_minutes": {
                    wc: load["open_minutes"].get(wc, 0.0) + exp.get(wc, 0.0) for wc in set(load["open_minutes"]) | set(exp)
                },
            }
            return leadtime.plan(route, per_part, 1, date.today(), wl, date.today(), 0, facts["policy"]).committed.isoformat()

        out["flood"] = {"like": f"email {big['email_id']} {big['part']} ({', '.join(route)})", "added": flood,
                        "committed_before": committed([]), "committed_after": committed([big] * flood)}  # fmt: skip
    print(json.dumps(out))
