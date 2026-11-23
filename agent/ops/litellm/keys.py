"""Register one LiteLLM virtual key per agent from agent/config/agents.yaml (ADR 0005). Idempotent.

The key VALUE is generated into .env by `make secrets` (LITELLM_KEY_<AGENT>) and registered here with its
allowed model aliases, rate limits and 30-day budget; a second run updates the limits. Talks to LiteLLM
through the Envoy ingress with the master key; never prints a key.

Usage: uv run python -m agent.ops.litellm.keys
"""

import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from legacy.odoo.rpc import ROOT, env

LITELLM = "https://localhost:8446"
REGISTRY = ROOT / "agent" / "config" / "agents.yaml"
CA = ROOT / "data" / "generated" / "certs" / "ca.crt"


def call(path: str, body: dict[str, Any] | None = None, bearer: str | None = None) -> tuple[int, dict[str, Any]]:
    """Keys travel only in the Authorization header, never in a URL (URLs end up in access logs)."""
    req = urllib.request.Request(LITELLM + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {bearer or env('LITELLM_MASTER_KEY')}",
                                          "Content-Type": "application/json"})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context(cafile=str(CA))) as r:  # nosec B310 (a fixed https URL to our own gateway)
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def registry(path: Path = REGISTRY) -> dict[str, dict[str, Any]]:
    agents: dict[str, dict[str, Any]] = yaml.safe_load(path.read_text(encoding="utf-8"))["agents"]
    return agents


def main() -> None:
    made, updated = [], []
    for name, a in registry().items():
        key = env(f"LITELLM_KEY_{name.upper()}")
        spec = {"key": key, "key_alias": f"agent-{name}", "models": a["models"], "rpm_limit": a["rpm"],
                "tpm_limit": a["tpm"], "max_budget": a["budget_usd"], "budget_duration": "30d",
                "metadata": {"agent": name, "owner": a["owner"], "risk": a["risk"]}}  # fmt: skip
        status, _ = call("/key/info", bearer=key)  # does this key exist? (asks as the key itself)
        if status == 200:
            spec.pop("key_alias")
            status, body = call("/key/update", spec)
            updated.append(name)
        else:
            status, body = call("/key/generate", spec)
            made.append(name)
        if status != 200:
            raise SystemExit(f"LiteLLM refused the key for {name}: {status} {str(body)[:200]}")
    print(f"litellm keys: {len(made)} created ({', '.join(made) or '-'}), {len(updated)} updated ({', '.join(updated) or '-'})")


if __name__ == "__main__":
    main()
