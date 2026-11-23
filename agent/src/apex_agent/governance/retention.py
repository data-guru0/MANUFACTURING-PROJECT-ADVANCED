"""Retention (S9 data governance), from policy.yaml -> retention:

  checkpoints   the graph's working memory of a FINISHED job (its prompts' inputs, drafts, the approval token): deleted
                after `checkpoint_days`. The job's results stay (extractions, estimates, decisions, actions, audit log).
  quarantine    files the security gate stopped (viruses, lying types): deleted after `quarantine_days`.
Kept on purpose: the raw emails (Apex's mail server keeps them anyway), the audit log (never), Odoo (Apex's system).
ponytail: Langfuse traces keep their own retention (a Langfuse EE feature); the drafts in them are Presidio-redacted.

Usage: python -m apex_agent.governance.retention            # dry run: what WOULD be deleted
       python -m apex_agent.governance.retention --apply
"""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import yaml
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from apex_agent import audit
from apex_agent.ingest.store import Store
from apex_agent.knowledge import db
from apex_agent.settings import DbSettings, load_as

FINISHED = ("done", "sent", "triaged", "failed")


def run(apply: bool, checkpoint_days: int, quarantine_days: int) -> dict[str, Any]:
    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute("""SELECT email_id FROM jobs WHERE status = ANY(%s) AND finished_at < now() - make_interval(days => %s)
                       AND email_id IN (SELECT DISTINCT replace(thread_id, 'email-', '')::bigint FROM checkpoints)""",
                    (list(FINISHED), checkpoint_days))  # fmt: skip
        jobs = [r[0] for r in cur.fetchall()]
    store = Store()
    cutoff = datetime.now(UTC) - timedelta(days=quarantine_days)
    old = [o.object_name for o in store.client.list_objects(store.bucket, prefix="quarantine/", recursive=True)
           if o.last_modified and o.last_modified < cutoff]  # fmt: skip
    if apply:
        cp = psycopg.connect(
            load_as(DbSettings).url.get_secret_value(), autocommit=True, prepare_threshold=0, row_factory=dict_row
        )
        saver = PostgresSaver(cp)
        for e in jobs:
            saver.delete_thread(f"email-{e}")
        for name in old:
            store.client.remove_object(store.bucket, name)
    out = {"applied": apply, "checkpoints_of_finished_jobs": len(jobs), "quarantined_files": len(old),
           "rules": {"checkpoint_days": checkpoint_days, "quarantine_days": quarantine_days}}  # fmt: skip
    if apply:
        audit.record("admin", "retention", {"event": "retention applied", **out})
    return out


def main() -> None:
    policy = yaml.safe_load(
        (Path(os.environ.get("APEX_CONFIG_DIR", "/app/agent-config")) / "policy.yaml").read_text(encoding="utf-8")
    )
    r = policy["retention"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--checkpoint-days", type=int, default=int(r["checkpoint_days"]))
    ap.add_argument("--quarantine-days", type=int, default=int(r["quarantine_days"]))
    a = ap.parse_args()
    print(json.dumps(run(a.apply, a.checkpoint_days, a.quarantine_days)))


if __name__ == "__main__":
    main()
