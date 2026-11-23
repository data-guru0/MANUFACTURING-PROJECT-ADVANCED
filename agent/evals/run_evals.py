"""S10: one command scores the AI parts of the running system and guards against regressions.

  triage, injection recall   stored results vs the labelled traffic          (S3 evaluator, no LLM)
  extraction                 stored results vs the drawings' ground truth     (S4 evaluator, no LLM)
  retrieval                  analog search on 80 held-out parts               (S2 evaluator, local embeddings)
  price, delivery date       held-out quotes and a delivery backtest          (S5 evaluator, code)
  draft faithfulness         share of quote drafts whose numbers all come from the computed facts (SQL)
  red team                   share of the red-team injections that got a red banner (SQL)
  golden triage (LIVE)       re-runs triage with the prompt in the code now, on 3 labelled emails; LiteLLM's cache
                             makes an unchanged prompt free, a changed prompt gets real answers (apex_agent.golden; 3 emails)

Writes agent/evals/last_run.json, pushes the scores to Langfuse (a trace per run, a score per metric) and to the
`eval_runs` table (Grafana "AI Quality"). Exit 1 when a metric is below its floor or regressed from the recorded
scores (agent/evals/scores.json) by more than the tolerance. `--record` makes this run the new reference.

Usage: uv run python -m agent.evals.run_evals [--record] [--skip-slow]
"""

import argparse
import json
import ssl
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from legacy.odoo.rpc import ROOT, env

HERE = Path(__file__).parent
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "apex",
    "-f",
    str(ROOT / "docker-compose.legacy.yml"),
    "-f",
    str(ROOT / "docker-compose.agent.yml"),
]


def last_json(cmd: list[str], stdin: str | None = None) -> dict[str, Any]:
    r = subprocess.run([*COMPOSE, *cmd], capture_output=True, text=True, encoding="utf-8", input=stdin, timeout=1800)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    if r.returncode != 0 or not lines:
        raise SystemExit(f"{' '.join(cmd[-3:])} failed: {r.stderr[-800:]}")
    out: dict[str, Any] = json.loads(lines[-1])
    return out


def sql(query: str) -> str:
    r = subprocess.run([*COMPOSE, "exec", "-T", "app-postgres", "psql", "-U", "postgres", "-d", "apex", "-At", "-c", query],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)  # fmt: skip
    return r.stdout.strip()


def golden_cases() -> list[dict[str, str]]:
    """3 fixed labelled emails (kept small: Groq free-tier quota): a clean RFQ, a clean non-RFQ, an exfiltration attempt."""
    labels = [
        json.loads(x)
        for x in (ROOT / "data" / "generated" / "rfq_labels.jsonl").read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    want = [("rfq", None, 1), ("non_rfq", None, 1), ("non_rfq", "exfiltration_injection", 1)]
    out = []
    for cat, attack, n in want:
        pick = [lab for lab in labels if lab["category"] == cat and lab["attack"] == attack][:n]
        out += [{"message_id": lab["message_id"], "expected": "rfq" if cat == "rfq" else "not_rfq"} for lab in pick]
    return out


def score(skip_slow: bool) -> dict[str, float | None]:
    k = ["run", "--rm", "knowledge", "python", "-m"]
    s: dict[str, float | None] = {}
    s["golden_triage_accuracy"] = last_json(["exec", "-T", "worker", "python", "-m", "apex_agent.golden"],
                                            json.dumps(golden_cases()))["golden_triage_accuracy"]  # fmt: skip
    t = last_json([*k, "apex_agent.evaluate_triage"])
    s["triage_accuracy"], s["injection_recall"] = t["triage_accuracy"], t["injection_recall"]
    if not skip_slow:
        x = last_json([*k, "apex_agent.documents.evaluate"])
        s["extraction_material"], s["extraction_envelope"], s["extraction_quantities"] = (
            x.get("material"),
            x.get("envelope"),
            x.get("quantities"),
        )
        s["retrieval_top5"] = last_json([*k, "apex_agent.knowledge.evaluate"])["top5_same_family_material_with_filter"]
        c = last_json([*k, "apex_agent.costing.evaluate"])
        s["price_mdape"], s["committed_date_met"] = c["price_mdape"], c["committed_met"]
    faithful = sql("""SELECT round(avg(((payload->'draft'->>'faithful')::boolean)::int), 3) FROM review_items
                      WHERE payload->'draft'->>'mode' = 'quote' AND email_id NOT IN (SELECT email_id FROM canaries)""")
    s["draft_faithfulness"] = float(faithful) if faithful else None
    flagged = sql("""SELECT round(avg(g.red_banner::int), 3) FROM emails e JOIN gate_results g ON g.email_id = e.id
                     WHERE e.subject ~ 'redteam-[0-9]+-(body|pdf|dxf)\\)'""")
    s["redteam_injection_flagged"] = float(flagged) if flagged else None
    return s


def judge(scores: dict[str, float | None], cfg: dict[str, Any], recorded: dict[str, float]) -> list[str]:
    problems = []
    for name, rule in cfg["metrics"].items():
        v = scores.get(name)
        if v is None:
            continue  # not measured in this run (e.g. --skip-slow, or nothing to score yet)
        lower = bool(rule.get("lower"))
        if (v > rule["floor"]) if lower else (v < rule["floor"]):
            problems.append(f"{name} = {v} is {'above' if lower else 'below'} its floor {rule['floor']}")
        if name in recorded:
            worse = (v - recorded[name]) if lower else (recorded[name] - v)
            if worse > cfg["tolerance"]:
                problems.append(f"{name} regressed: {recorded[name]} -> {v}")
    return problems


def publish(scores: dict[str, float | None], problems: list[str], run_id: str) -> None:
    """Langfuse (a trace + one score per metric) and the eval_runs table (Grafana)."""
    tls = ssl.create_default_context(cafile=str(ROOT / "data" / "generated" / "certs" / "ca.crt"))
    auth = (env("LANGFUSE_PUBLIC_KEY"), env("LANGFUSE_SECRET_KEY"))
    now = datetime.now(UTC).isoformat()
    batch = [{"id": uuid.uuid4().hex, "type": "trace-create", "timestamp": now,
              "body": {"id": run_id, "name": "s10-eval-run", "tags": ["eval"], "metadata": {"problems": problems}}}]  # fmt: skip
    batch += [{"id": uuid.uuid4().hex, "type": "score-create", "timestamp": now,
               "body": {"id": uuid.uuid4().hex, "traceId": run_id, "name": k, "value": v, "dataType": "NUMERIC"}}
              for k, v in scores.items() if v is not None]  # fmt: skip
    httpx.post(
        "https://localhost:8443/api/public/ingestion", json={"batch": batch}, auth=auth, verify=tls, timeout=30
    ).raise_for_status()
    values = ",".join(f"('{run_id}', '{k}', {v}, {'false' if problems else 'true'})" for k, v in scores.items() if v is not None)
    sql(f"INSERT INTO eval_runs (run_id, metric, value, passed) VALUES {values}")  # nosec B608 (our own metric names and numbers)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true", help="make this run the reference for regressions")
    ap.add_argument("--skip-slow", action="store_true", help="only the fast metrics (golden, triage, faithfulness, red team)")
    a = ap.parse_args()
    cfg = yaml.safe_load((HERE / "thresholds.yaml").read_text(encoding="utf-8"))
    recorded_path = HERE / "scores.json"
    recorded = json.loads(recorded_path.read_text(encoding="utf-8")) if recorded_path.exists() else {}
    scores = score(a.skip_slow)
    problems = judge(scores, cfg, recorded)
    run_id = uuid.uuid4().hex
    publish(scores, problems, run_id)
    (HERE / "last_run.json").write_text(
        json.dumps({"run_id": run_id, "scores": scores, "problems": problems}, indent=1) + "\n", encoding="utf-8"
    )
    if a.record and not problems:
        recorded_path.write_text(
            json.dumps({k: v for k, v in scores.items() if v is not None}, indent=1) + "\n", encoding="utf-8"
        )
    print(json.dumps({"scores": scores, "problems": problems, "recorded": a.record and not problems}, indent=1))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
