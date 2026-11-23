"""learn, part 2 (S8): what happened to the quotes the agent sent. A person confirms (won) or cancels (lost) the
draft quotation in Odoo as usual; the worker reads those states back every few minutes (erp-mcp-read, node `learn`)
and keeps them in `outcomes`. That is the label later phases learn from (S14 win probability, S18 quoted vs actual).

Usage: python -m apex_agent.actions.outcomes     # one sync now, prints what changed
"""

import asyncio
import json
from typing import Any

from apex_agent.knowledge import db
from apex_agent.tools import call_tool

OUTCOME = {"sale": "won", "done": "won", "cancel": "lost"}


def sync(conn: Any) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT email_id FROM actions WHERE result ? 'quotation'")
        ids = [r[0] for r in cur.fetchall()]
    if not ids:
        return {"quotations": 0, "changed": []}
    ok, rows = asyncio.run(call_tool("erp-mcp-read", "quote_status", {"refs": [f"AGENT-RFQ-{i}" for i in ids]}, "learn"))
    if not ok:
        return {"error": str(rows)[:300]}
    changed = []
    with conn.cursor() as cur:
        for r in rows:
            e = int(r["client_order_ref"].rsplit("-", 1)[1])
            outcome = OUTCOME.get(r["state"], "open")
            cur.execute("""INSERT INTO outcomes (email_id, quotation, state, outcome, lost_reason, amount_usd) VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (email_id) DO UPDATE SET state = EXCLUDED.state, outcome = EXCLUDED.outcome,
                             lost_reason = EXCLUDED.lost_reason, amount_usd = EXCLUDED.amount_usd, changed_at = now()
                           WHERE outcomes.state IS DISTINCT FROM EXCLUDED.state
                           RETURNING email_id""",
                        (e, r["name"], r["state"], outcome, r.get("x_lost_reason") or None, r.get("amount_untaxed")))  # fmt: skip
            if cur.fetchone():
                changed.append({"email_id": e, "quotation": r["name"], "outcome": outcome})
    conn.commit()
    return {"quotations": len(rows), "changed": changed}


if __name__ == "__main__":
    print(json.dumps(sync(db.connect())))
