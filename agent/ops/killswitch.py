"""The AI kill switch (S9): one command drops the whole system into MANUAL mode, or back.

  off: `ai_enabled: false` in agent/config/policy.yaml + OPA restarted (every AI step is denied: apex/system/ai_enabled),
       AND every agent's LiteLLM key is blocked (even a call that skipped OPA is refused at the gateway).
  on:  the reverse.
Jobs don't fail meanwhile: the worker gets AiDisabled and re-queues them; the estimators work the legacy way (Roundcube,
Odoo, the Excel calculator) exactly as before the engagement. Every switch is written to the audit log with a name.

Usage: uv run python -m agent.ops.killswitch off --by Vikram --reason "suspected prompt injection campaign"
       uv run python -m agent.ops.killswitch on  --by Vikram --reason "campaign blocked at the gate"
       uv run python -m agent.ops.killswitch status
"""

import argparse
import json
import re
import subprocess

from agent.ops.litellm.keys import call, registry
from legacy.odoo.rpc import ROOT, env

POLICY = ROOT / "agent" / "config" / "policy.yaml"
COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(ROOT / "docker-compose.legacy.yml"), "-f",
           str(ROOT / "docker-compose.agent.yml")]  # fmt: skip


def keys_state() -> dict[str, bool]:
    """agent -> blocked? (LiteLLM's own view of each agent's key)"""
    out = {}
    for name in registry():
        status, body = call("/key/info", bearer=env(f"LITELLM_KEY_{name.upper()}"))
        out[name] = status != 200 or bool((body.get("info") or {}).get("blocked"))
    return out


def switch(enabled: bool, by: str, reason: str) -> dict[str, object]:
    text = POLICY.read_text(encoding="utf-8")
    new, n = re.subn(r"(?m)^ai_enabled: (true|false)\b", f"ai_enabled: {'true' if enabled else 'false'}", text)
    if n != 1:
        raise SystemExit("policy.yaml has no single top-level `ai_enabled:` line")
    POLICY.write_text(new, encoding="utf-8")
    r = subprocess.run([*COMPOSE, "up", "-d", "--wait", "--force-recreate", "opa"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"OPA restart failed: {r.stderr[-400:]}")
    for name in registry():
        status, body = call("/key/unblock" if enabled else "/key/block", {"key": env(f"LITELLM_KEY_{name.upper()}")})
        if status != 200:
            raise SystemExit(f"LiteLLM refused to {'unblock' if enabled else 'block'} {name}: {status} {str(body)[:200]}")
    rec = {"ai_enabled": enabled, "by": by, "reason": reason, "keys_blocked": keys_state()}
    audit_admin("kill switch " + ("off (AI back on)" if enabled else "ON (manual mode)"), by, rec)
    return rec


def audit_admin(event: str, by: str, data: dict[str, object]) -> None:
    """Admin actions run on the host, which can't reach the data network: the row goes in through psql (the trigger
    still computes the chain)."""
    payload = json.dumps({"event": event, **data})
    sql = "INSERT INTO audit_log (kind, actor, data) VALUES ('admin', :'by', :'payload'::jsonb);"
    r = subprocess.run([*COMPOSE, "exec", "-T", "app-postgres", "psql", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "apex",
                        "-v", f"by={by}", "-v", f"payload={payload}"], input=sql, capture_output=True, text=True)  # fmt: skip
    if r.returncode != 0:
        raise SystemExit(f"audit failed: {r.stderr[-300:]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("state", choices=["on", "off", "status"])
    ap.add_argument("--by", default="")
    ap.add_argument("--reason", default="")
    a = ap.parse_args()
    if a.state == "status":
        on = bool(re.search(r"(?m)^ai_enabled: true\b", POLICY.read_text(encoding="utf-8")))
        print(json.dumps({"ai_enabled": on, "keys_blocked": keys_state()}))
        return
    if not a.by.strip() or not a.reason.strip():
        raise SystemExit("--by <name> and --reason <why> are required: the kill switch is a named, logged decision")
    print(json.dumps(switch(a.state == "on", a.by.strip(), a.reason.strip())))


if __name__ == "__main__":
    main()
