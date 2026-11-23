"""Budget alerts (S9 cost governance). LiteLLM enforces each agent's 30-day budget itself (max_budget on its key: at
100 % it refuses the calls). This raises the warnings BEFORE that: at 50, 80 and 100 % of the budget, once per budget
period, as a log line, an audit row and a `budget_alerts` row (Grafana). The worker checks every 2 minutes.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from apex_agent import audit
from apex_agent.security.pii import log

THRESHOLDS = (50, 80, 100)


def check(conn: psycopg.Connection[Any], clients: dict[str, Any]) -> list[dict[str, Any]]:
    """`clients`: agent name -> its LLMClient (each asks LiteLLM about ITS OWN key: no master key needed)."""
    raised = []
    for agent, c in clients.items():
        r = c.http.get("/key/info")
        if r.status_code != 200:
            continue  # a blocked key (kill switch) or LiteLLM down: nothing to compare
        info = r.json().get("info") or {}
        spend, budget = float(info.get("spend") or 0.0), float(info.get("max_budget") or 0.0)
        if budget <= 0:
            continue
        reset = info.get("budget_reset_at")
        period_end = datetime.fromisoformat(reset.replace("Z", "+00:00")) if reset else datetime.now(UTC) + timedelta(days=30)
        period_start = (period_end - timedelta(days=30)).date()
        for t in THRESHOLDS:
            if spend < budget * t / 100:
                continue
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO budget_alerts (agent, period_start, threshold, spend_usd, budget_usd) VALUES (%s,%s,%s,%s,%s)
                               ON CONFLICT DO NOTHING RETURNING agent""", (agent, period_start, t, spend, budget))  # fmt: skip
                new = cur.fetchone()
            conn.commit()
            if new:
                alert = {"agent": agent, "threshold_pct": t, "spend_usd": round(spend, 4), "budget_usd": budget}
                log("budget alert", **alert)
                audit.record("admin", "cost-governance", {"event": "budget alert", **alert})
                raised.append(alert)
    return raised
