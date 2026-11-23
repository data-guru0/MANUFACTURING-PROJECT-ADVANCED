"""The governance report (S9): what the AI costs per quote and per won order, the ROI, and how the humans oversee it.
Writes docs/fde/GOVERNANCE_REPORT.md from the agent's database and the policy (assumptions are named in the report).

Usage (knowledge job): python -m apex_agent.governance.report
"""

import os
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

import yaml

from apex_agent.knowledge import db


def q(conn: Any, sql: str) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def build(conn: Any, policy: dict[str, Any]) -> str:
    roi = policy["governance"]["roi"]
    spend = dict(q(conn, "SELECT email_id, sum(spent_usd) FROM graph_events GROUP BY 1"))
    reviewed = [r[0] for r in q(conn, "SELECT email_id FROM review_items WHERE email_id NOT IN (SELECT email_id FROM canaries)")]
    sent = [r[0] for r in q(conn, "SELECT DISTINCT email_id FROM actions WHERE result ? 'quotation'")]
    won = [r[0] for r in q(conn, "SELECT email_id FROM outcomes WHERE outcome = 'won'")]
    lost = [r[0] for r in q(conn, "SELECT email_id FROM outcomes WHERE outcome = 'lost'")]
    total = sum(float(v) for v in spend.values())

    def per(ids: list[int]) -> str:
        return f"${sum(float(spend.get(i, 0)) for i in ids) / len(ids):.4f}" if ids else "n/a (none yet)"

    saved_h = len(sent) * (roi["baseline_minutes_per_quote"] - roi["review_minutes_per_quote"]) / 60
    saved_usd = saved_h * roi["estimator_rate_usd_per_h"]
    ai_sent = sum(float(spend.get(i, 0)) for i in sent)
    by_reviewer = q(
        conn,
        """
        SELECT d.decided_by, count(*), count(*) FILTER (WHERE d.action IN ('edit', 'recost')),
               array_agg(extract(epoch FROM d.decided_at - r.updated_at) / 60)
        FROM decisions d JOIN review_items r USING (email_id)
        WHERE d.email_id NOT IN (SELECT email_id FROM canaries) AND d.decided_by = lower(d.decided_by) GROUP BY 1 ORDER BY 1""",
    )  # screen users are lower-case
    recosts = dict(q(conn, "SELECT by_user, count(DISTINCT email_id) FROM feedback WHERE field LIKE '%.%' GROUP BY 1"))
    canaries = {
        r[0]: (r[1], r[2])
        for r in q(
            conn,
            """SELECT decided_by, count(*) FILTER (WHERE result = 'caught'),
        count(*) FILTER (WHERE result = 'missed') FROM canaries WHERE result IS NOT NULL GROUP BY 1""",
        )
    }
    alerts = q(
        conn, "SELECT agent, threshold, round(spend_usd::numeric, 4), budget_usd, at FROM budget_alerts ORDER BY at DESC LIMIT 20"
    )
    lines = [
        "# Governance report: AI cost, ROI and human oversight",
        "",
        f"_Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `python -m apex_agent.governance.report` from the agent's database. Regenerate any time._",
        "",
        "## Cost per quote",
        "LLM spend per job is LiteLLM's own cost figure (`x-litellm-response-cost`), summed by the graph (S6). Canary copies are excluded.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| AI spend, all jobs through the graph | ${total:.4f} over {len(spend)} jobs |",
        f"| AI $ per job that reached review | {per(reviewed)} ({len(reviewed)} jobs) |",
        f"| AI $ per quote sent | {per(sent)} ({len(sent)} sent) |",
        f"| AI $ per won order | {f'${ai_sent / len(won):.4f}' if won else 'n/a (no order won yet)'} ({len(won)} won, {len(lost)} lost) |",
        "",
        "## ROI (assumptions from `policy.yaml -> governance.roi`)",
        f"- Estimator time per quote before: **{roi['baseline_minutes_per_quote']} min** ({roi['baseline_source']}).",
        f"- Review time per quote now: **{roi['review_minutes_per_quote']} min** ({roi['review_source']}).",
        f"- Estimator cost: **${roi['estimator_rate_usd_per_h']}/h** ({roi['rate_source']}).",
        f"- For the {len(sent)} quotes sent: **{saved_h:.1f} estimator hours saved ≈ ${saved_usd:,.2f}**, against **${ai_sent:.4f}** of AI spend"
        + (f" (≈ {saved_usd / ai_sent:,.0f}×)." if ai_sent > 0 else "."),
        "",
        "## Human oversight (review screen decisions; canaries excluded)",
        "Waiting time = from the moment the job reached review to the decision (queue time, not effort).",
        "",
        "| Reviewer | Decisions | Edited or re-costed | Jobs re-costed | Median wait (min) | Canaries caught / missed |",
        "|---|---|---|---|---|---|",
        *[
            f"| {who} | {n} | {e} ({100 * e / n:.0f} %) | {recosts.get(who, 0)} | {median(float(x) for x in waits):.1f} | "
            f"{canaries.get(who, (0, 0))[0]} / {canaries.get(who, (0, 0))[1]} |"
            for who, n, e, waits in by_reviewer
        ],  # fmt: skip
        "",
        "A reviewer who approves every canary is rubber-stamping (see `docs/governance/HUMAN_OVERSIGHT.md`).",
        "",
        "## Budget alerts (50 / 80 / 100 % of an agent's 30-day budget)",
        "",
        "| Agent | Threshold | Spend | Budget | When |",
        "|---|---|---|---|---|",
        *[f"| {a} | {t} % | ${s} | ${b} | {at:%Y-%m-%d %H:%M} |" for a, t, s, b, at in alerts],
        *([] if alerts else ["| (none: every agent is below 50 % of its budget) | | | | |"]),
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    cfg = Path(os.environ.get("APEX_CONFIG_DIR", "/app/agent-config"))
    policy = yaml.safe_load((cfg / "policy.yaml").read_text(encoding="utf-8"))
    out = Path(os.environ.get("APEX_DOCS_DIR", "/app/docs-fde")) / "GOVERNANCE_REPORT.md"
    out.write_text(build(db.connect(), policy), encoding="utf-8")
    print(f"wrote {out}")
