"""The Grafana dashboards (S10, plus S14 capacity and S18 variance), generated from the SQL below (edit here, run).
The L6 "before" numbers are read from docs/legacy/BASELINE_METRICS.md, so `make baseline` regenerates them.

Operations · Business KPIs before/after · AI Quality · AI Cost & Governance · Human Oversight.
Data: the agent's database through the read-only role `grafana_ro` (datasource uid apex-pg); Envoy metrics from
Prometheus. The "before" numbers are the L6 baseline (docs/legacy/BASELINE_METRICS.md), measured on the legacy system.

Usage: uv run python -m agent.ops.grafana.make_dashboards
"""

import json
import re
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent / "provisioning" / "dashboards"
PG = {"type": "grafana-postgresql-datasource", "uid": "apex-pg"}
PROM = {"type": "prometheus", "uid": "prometheus"}


def baseline() -> dict[str, str]:
    """The L6 "before" numbers, read from the generated baseline report (never typed here: they move with each reset)."""
    md = (Path(__file__).resolve().parents[3] / "docs" / "legacy" / "BASELINE_METRICS.md").read_text(encoding="utf-8")

    def cell(label: str) -> str:
        m = re.search(r"\| \*\*" + re.escape(label) + r"\*\*[^|]*\| ([^|]+) \|", md)
        if not m:
            raise SystemExit(f"{label!r} not found in BASELINE_METRICS.md: run `make baseline` first")
        return m.group(1).replace("**", "").replace("'", "").strip()

    return {"median": cell("Median response time"), "win": cell("Win rate"), "on_time": cell("On-time delivery")}


B = baseline()


def panel(
    title: str, kind: str, sql: str, w: int = 12, h: int = 8, unit: str | None = None, prom: bool = False
) -> dict[str, Any]:
    target = {"refId": "A", "datasource": PROM, "expr": sql} if prom else \
        {"refId": "A", "datasource": PG, "rawSql": sql, "format": "time_series" if kind == "timeseries" else "table", "rawQuery": True, "editorMode": "code"}  # fmt: skip
    p: dict[str, Any] = {
        "title": title,
        "type": kind,
        "datasource": PROM if prom else PG,
        "gridPos": {"w": w, "h": h},
        "targets": [target],
    }
    if unit:
        p["fieldConfig"] = {"defaults": {"unit": unit}, "overrides": []}
    return p


def dashboard(uid: str, title: str, panels: list[dict[str, Any]], refresh: str = "30s") -> dict[str, Any]:
    x = y = row_h = 0
    for p in panels:  # simple flow layout, 24 columns wide
        if x + p["gridPos"]["w"] > 24:
            x, y, row_h = 0, y + row_h, 0
        p["gridPos"].update({"x": x, "y": y})
        x += p["gridPos"]["w"]
        row_h = max(row_h, p["gridPos"]["h"])
    for i, p in enumerate(panels, 1):
        p["id"] = i
    return {"uid": uid, "title": title, "tags": ["apex", "s10"], "timezone": "browser", "refresh": refresh, "schemaVersion": 39,
            "time": {"from": "now-24h", "to": "now"}, "panels": panels}  # fmt: skip


REAL = "e.mailbox <> 'canary'"  # canary copies are not real traffic

DASHBOARDS = [
    dashboard(
        "apex-ops",
        "Apex · Operations",
        [
            panel(
                "Jobs by status now", "table", "SELECT status, count(*) AS jobs FROM jobs GROUP BY status ORDER BY jobs DESC", 8
            ),
            panel(
                "Oldest job waiting to run (minutes)",
                "stat",
                "SELECT coalesce(round(extract(epoch FROM now() - min(created_at)) / 60), 0) AS minutes FROM jobs WHERE status = 'queued' AND available_at <= now()",
                4,
                unit="m",
            ),
            panel("Waiting for review now", "stat", "SELECT count(*) FROM jobs WHERE status = 'awaiting_review'", 4),
            panel(
                "Jobs waiting for Groq's daily quota",
                "stat",
                "SELECT count(*) FROM jobs WHERE status = 'queued' AND error LIKE 'QuotaExhausted%'",
                4,
            ),
            panel("Failed jobs (need a person)", "stat", "SELECT count(*) FROM jobs WHERE status = 'failed'", 4),
            panel(
                "Emails in per hour",
                "timeseries",
                "SELECT date_trunc('hour', e.ingested_at) AS time, count(*) AS emails FROM emails e WHERE $__timeFilter(e.ingested_at) AND "  # nosec B608 (a constant panel query for Grafana: no input reaches it)
                + REAL
                + " GROUP BY 1 ORDER BY 1",
                12,
            ),
            panel(
                "Graph steps finished per hour, by node",
                "timeseries",
                "SELECT date_trunc('hour', at) AS time, node AS metric, count(*) AS steps FROM graph_events WHERE $__timeFilter(at) GROUP BY 1, 2 ORDER BY 1",
                12,
            ),
            panel(
                "Minutes from email in to waiting for review (median, per hour)",
                "timeseries",
                """SELECT date_trunc('hour', r.updated_at) AS time,
                        percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM r.updated_at - e.ingested_at) / 60) AS minutes
                 FROM review_items r JOIN emails e ON e.id = r.email_id WHERE $__timeFilter(r.updated_at) AND """
                + REAL  # nosec B608 (a constant panel query for Grafana: no input reaches it)
                + " GROUP BY 1 ORDER BY 1",
                12,
                unit="m",
            ),
            panel(
                "Recent job errors",
                "table",
                "SELECT email_id AS email, status, attempts, left(error, 120) AS error, available_at FROM jobs WHERE error IS NOT NULL ORDER BY available_at DESC LIMIT 30",
                12,
            ),
            panel(
                "Envoy ingress: requests per second by response class",
                "timeseries",
                'sum by (envoy_response_code_class) (rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix=~"ingress_.*"}[5m]))',
                24,
                prom=True,
            ),
        ],
    ),
    dashboard(
        "apex-kpi",
        "Apex · Business KPIs (before / after)",
        [
            panel(
                "Median response time: before (legacy, L6) vs now (agent)",
                "table",
                """SELECT 'before: by hand (L6 baseline, 400 quotes)' AS process, '"""  # nosec B608 (the L6 baseline value is read from our generated report and stripped of quotes)
                + B["median"]
                + """' AS median_rfq_to_quote
                 UNION ALL
                 SELECT 'now: with the agent (' || count(*) || ' quotes sent)',
                        coalesce(round((percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM a.at - coalesce(e.sent_at, e.ingested_at)) / 3600))::numeric, 2) || ' hours', 'none sent yet')
                 FROM actions a JOIN emails e ON e.id = a.email_id WHERE a.result ? 'quotation'""",
                12,
            ),
            panel(
                "Win rate: before vs now",
                "table",
                """SELECT 'before (L6 baseline)' AS process, '"""  # nosec B608 (the L6 baseline value is read from our generated report and stripped of quotes)
                + B["win"]
                + """' AS win_rate
                 UNION ALL
                 SELECT 'now (agent quotes decided in Odoo)',
                        CASE WHEN count(*) FILTER (WHERE outcome <> 'open') = 0 THEN 'no decided quotes yet'
                             ELSE round(100.0 * count(*) FILTER (WHERE outcome = 'won') / count(*) FILTER (WHERE outcome <> 'open'), 1) || ' % ('
                                  || count(*) FILTER (WHERE outcome = 'won') || ' won / ' || count(*) FILTER (WHERE outcome <> 'open') || ' decided)' END
                 FROM outcomes""",
                12,
            ),
            panel(
                "On-time delivery: before vs now",
                "table",
                """SELECT 'before (L6 baseline, flat 3-week promises)' AS process, '"""  # nosec B608 (the L6 baseline value is read from our generated report and stripped of quotes)
                + B["on_time"]
                + """' AS on_time
                 UNION ALL SELECT 'now (committed dates from capacity)', 'measurable once agent-quoted orders are delivered (S18 compares)'""",
                12,
            ),
            panel(
                "Quotes sent per day by the agent",
                "timeseries",
                "SELECT date_trunc('day', at) AS time, count(*) AS quotes FROM actions WHERE result ? 'quotation' AND $__timeFilter(at) GROUP BY 1 ORDER BY 1",
                12,
            ),
            panel(
                "Hours from RFQ to quote, per quote sent",
                "table",
                """SELECT a.email_id AS email, e.subject, round(extract(epoch FROM a.at - coalesce(e.sent_at, e.ingested_at)) / 3600, 2) AS hours,
                        a.result->>'quotation' AS quotation FROM actions a JOIN emails e ON e.id = a.email_id
                 WHERE a.result ? 'quotation' ORDER BY a.at DESC LIMIT 30""",
                24,
            ),
        ],
        refresh="1m",
    ),
    dashboard(
        "apex-quality",
        "Apex · AI Quality",
        [
            panel(
                "Latest evaluation scores (make evals)",
                "table",
                """SELECT DISTINCT ON (metric) metric, round(value::numeric, 3) AS value, passed AS run_passed, at
                 FROM eval_runs ORDER BY metric, at DESC""",
                12,
                10,
            ),
            panel(
                "Evaluation scores over time",
                "timeseries",
                "SELECT at AS time, metric, value FROM eval_runs WHERE $__timeFilter(at) ORDER BY at",
                12,
                10,
            ),
            panel(
                "Drafts whose every number is in the calculation (faithful), %",
                "stat",
                """SELECT round(100 * avg(((payload->'draft'->>'faithful')::boolean)::int), 1) FROM review_items
                 WHERE payload->'draft'->>'mode' = 'quote' AND email_id NOT IN (SELECT email_id FROM canaries)""",
                6,
                unit="percent",
            ),
            panel(
                "Values read with low confidence per RFQ (avg)",
                "stat",
                "SELECT round(avg(jsonb_array_length(coalesce(result->'needs_human', '[]'::jsonb))), 2) FROM extractions",
                6,
            ),
            panel(
                "Routes: quote / clarify / escalate",
                "piechart",
                "SELECT route AS metric, count(*) AS jobs FROM review_items WHERE email_id NOT IN (SELECT email_id FROM canaries) GROUP BY route",
                6,
            ),
            panel(
                "Human edits per field (what the AI gets wrong)",
                "table",
                "SELECT regexp_replace(field, '^[^.]+\\.', '') AS field, count(*) AS edits FROM feedback GROUP BY 1 ORDER BY 2 DESC",
                6,
            ),
        ],
        refresh="1m",
    ),
    dashboard(
        "apex-cost",
        "Apex · AI Cost & Governance",
        [
            panel(
                "LLM spend per day by agent ($)",
                "timeseries",
                """SELECT date_trunc('day', at) AS time, actor AS metric, sum((data->>'cost_usd')::float) AS usd FROM audit_log
                 WHERE kind = 'llm_call' AND $__timeFilter(at) GROUP BY 1, 2 ORDER BY 1""",
                12,
                unit="currencyUSD",
            ),
            panel(
                "LLM spend per job reaching review ($, median)",
                "stat",
                """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY s) FROM (SELECT sum(spent_usd) AS s FROM graph_events
                 WHERE email_id IN (SELECT email_id FROM review_items) GROUP BY email_id) x""",
                6,
                unit="currencyUSD",
            ),
            panel(
                "Calls answered from the cache, %",
                "stat",
                "SELECT round(100 * avg((data->>'cache_hit')::boolean::int), 1) FROM audit_log WHERE kind = 'llm_call' AND $__timeFilter(at)",
                6,
                unit="percent",
            ),
            panel(
                "Budget alerts (50 / 80 / 100 %)",
                "table",
                "SELECT at, agent, threshold AS pct, round(spend_usd::numeric, 4) AS spend_usd, budget_usd FROM budget_alerts ORDER BY at DESC",
                12,
            ),
            panel(
                "Kill switch and other admin actions",
                "table",
                "SELECT at, actor AS by, data->>'event' AS event, data->>'reason' AS reason FROM audit_log WHERE kind = 'admin' ORDER BY id DESC LIMIT 30",
                12,
            ),
            panel(
                "OPA denials in the last 24 h, by rule",
                "table",
                """SELECT data->>'rule' AS rule, count(*) AS denials FROM audit_log WHERE kind = 'opa_decision' AND at > now() - interval '24 hours'
                 AND (data->>'result' IN ('false', 'deny') OR data->'result'->>'allow' = 'false') GROUP BY 1 ORDER BY 2 DESC""",
                12,
            ),
            panel(
                "Audit log rows per kind",
                "table",
                "SELECT kind, count(*) AS rows, max(at) AS latest FROM audit_log GROUP BY kind ORDER BY rows DESC",
                12,
            ),
        ],
        refresh="1m",
    ),
    dashboard(
        "apex-oversight",
        "Apex · Human Oversight",
        [
            panel(
                "Decisions per reviewer",
                "table",
                """SELECT d.decided_by AS reviewer, count(*) AS decisions, count(*) FILTER (WHERE d.action = 'approve') AS approved,
                        count(*) FILTER (WHERE d.action IN ('edit', 'reject', 'clarify')) AS changed_or_stopped,
                        round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM d.decided_at - r.updated_at) / 60)::numeric, 1) AS median_wait_min
                 FROM decisions d JOIN review_items r USING (email_id) WHERE d.decided_by = lower(d.decided_by)
                   AND d.email_id NOT IN (SELECT email_id FROM canaries) GROUP BY 1 ORDER BY 2 DESC""",
                12,
            ),
            panel(
                "Canaries caught / missed per reviewer (rubber-stamp check)",
                "table",
                """SELECT decided_by AS reviewer, count(*) FILTER (WHERE result = 'caught') AS caught, count(*) FILTER (WHERE result = 'missed') AS missed,
                        count(*) FILTER (WHERE result IS NULL) AS waiting FROM canaries GROUP BY 1 ORDER BY 1""",
                12,
            ),
            panel(
                "Re-costs and edits by reviewer (field, was, now, why)",
                "table",
                "SELECT at, by_user AS reviewer, field, left(old_value::text, 40) AS was, left(new_value::text, 40) AS now, reason FROM feedback ORDER BY at DESC LIMIT 50",
                24,
            ),
        ],
        refresh="1m",
    ),
    dashboard(  # S18 (W14): quoted vs actual, from `python -m apex_agent.costing.variance`
        "apex-variance",
        "Apex · Quoted vs actual",
        [
            panel("Finished jobs", "stat", "SELECT count(*) AS jobs FROM variance_jobs", 6, 4),
            panel(
                "Jobs that lost money", "stat", "SELECT count(*) FILTER (WHERE lost_money) AS lost_money FROM variance_jobs", 6, 4
            ),
            panel(
                "Total loss on those jobs",
                "stat",
                "SELECT -sum(profit) FILTER (WHERE lost_money) AS loss FROM variance_jobs",
                6,
                4,
                "currencyUSD",
            ),
            panel(
                "Actual / planned minutes (all work orders)",
                "stat",
                "SELECT round(sum(minutes_actual) / nullif(sum(minutes_planned), 0), 3) AS ratio FROM variance_ops",
                6,
                4,
            ),
            panel(
                "Actual / planned minutes by family (median; > 1 = the estimate is too short)",
                "barchart",
                """SELECT family, round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ratio)::numeric, 3) AS median_ratio
                     FROM variance_ops WHERE family IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""",
                12,
            ),
            panel(
                "… by machine",
                "barchart",
                """SELECT work_center, round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ratio)::numeric, 3) AS median_ratio
                     FROM variance_ops GROUP BY 1 ORDER BY 2 DESC""",
                12,
            ),
            panel(
                "… by material",
                "table",
                """SELECT material, round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ratio)::numeric, 3) AS median_ratio, count(*) AS work_orders
                     FROM variance_ops WHERE material IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""",
                12,
            ),
            panel(
                "… by customer (profit)",
                "table",
                """SELECT customer, count(*) AS jobs, count(*) FILTER (WHERE lost_money) AS lost_money, round(sum(profit)) AS profit_usd
                     FROM variance_jobs GROUP BY 1 ORDER BY 4""",
                12,
            ),
            panel(
                "The jobs that lost money (worst first)",
                "table",
                """SELECT sale_order, customer, part, family, material, qty, revenue, actual_cost, profit, minutes_planned, minutes_actual
                     FROM variance_jobs WHERE lost_money ORDER BY profit""",
                24,
                10,
            ),
        ],
        refresh="5m",
    ),
]

if __name__ == "__main__":
    for d in DASHBOARDS:
        (OUT / f"{d['uid']}.json").write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(DASHBOARDS)} dashboards to {OUT}")
