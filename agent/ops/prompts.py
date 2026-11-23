"""Prompt and model versions under change control (S9 AI governance), kept in Langfuse's prompt management.

Every prompt the agents use, and the model list (agent/config/models.yaml), is a versioned "prompt" in Langfuse.
  push      a changed text becomes a new version labelled `candidate` (the very first version is `production`: bootstrap)
  promote   a named reviewer moves `production` to a version, with an evaluation report as evidence that is NEWER than
            the version (an eval run on the candidate); the promotion is written to the audit log
  check     the code must equal the `production` version, prompt by prompt: `make check` fails on any drift, so a
            prompt edit or a model swap can't ship without push -> eval -> promote

The prompts are read from the source files as literals (no import of the agent's heavy dependencies on the host).

Usage: uv run python -m agent.ops.prompts push
       uv run python -m agent.ops.prompts promote <name> <version> --by <reviewer> --evidence docs/fde/TRIAGE_EVAL.md
       uv run python -m agent.ops.prompts check
"""

import argparse
import ast
import hashlib
import json
import ssl
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from agent.ops.killswitch import audit_admin
from legacy.odoo.rpc import ROOT, env

LANGFUSE = "https://localhost:8443/api/public/v2/prompts"
SRC = ROOT / "agent" / "src" / "apex_agent"
PROMPTS = {  # Langfuse name -> (file, constant)
    "apex-triage": (SRC / "triage.py", "SYSTEM"),
    "apex-extract-email": (SRC / "documents" / "extract.py", "EMAIL_SYSTEM"),
    "apex-extract-critic": (SRC / "documents" / "extract.py", "CRITIC_SYSTEM"),
    "apex-vision": (SRC / "documents" / "scan.py", "VISION_PROMPT"),
    "apex-drafter": (SRC / "graph" / "draft.py", "SYSTEM"),
    "apex-rules": (SRC / "knowledge" / "rules.py", "SYSTEM_TEMPLATE"),
}
MODELS = ("apex-models", ROOT / "agent" / "config" / "models.yaml")


def literal(path: Path, name: str) -> str:
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise SystemExit(f"{path.name}: no string constant {name}")


def texts() -> dict[str, str]:
    out = {name: literal(f, const) for name, (f, const) in PROMPTS.items()}
    out[MODELS[0]] = MODELS[1].read_text(encoding="utf-8")
    return out


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def client() -> httpx.Client:
    tls = ssl.create_default_context(cafile=str(ROOT / "data" / "generated" / "certs" / "ca.crt"))
    return httpx.Client(auth=(env("LANGFUSE_PUBLIC_KEY"), env("LANGFUSE_SECRET_KEY")), verify=tls, timeout=30)


def version(c: httpx.Client, name: str, label: str) -> dict[str, Any] | None:
    r = c.get(f"{LANGFUSE}/{name}", params={"label": label})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    found: dict[str, Any] = r.json()
    return found


def push() -> dict[str, str]:
    out = {}
    with client() as c:
        for name, text in texts().items():
            latest, prod = version(c, name, "latest"), version(c, name, "production")
            if latest and latest["prompt"] == text:
                out[name] = f"unchanged (v{latest['version']})"
                continue
            labels = ["candidate"] if prod else ["production"]
            r = c.post(LANGFUSE, json={"name": name, "type": "text", "prompt": text, "labels": labels,
                                       "commitMessage": "bootstrap" if not prod else "changed in code; needs an eval and a reviewer",
                                       "config": {"sha256": sha(text)}})  # fmt: skip
            r.raise_for_status()
            out[name] = f"v{r.json()['version']} {labels[0]}"
    return out


def promote(name: str, ver: int, by: str, evidence: Path) -> dict[str, Any]:
    if not by.strip():
        raise SystemExit("--by <reviewer> is required")
    if not evidence.is_file():
        raise SystemExit(f"no evaluation report at {evidence}")
    with client() as c:
        r = c.get(f"{LANGFUSE}/{name}", params={"version": ver})
        r.raise_for_status()
        made = datetime.fromisoformat(r.json()["createdAt"].replace("Z", "+00:00")).timestamp()
        if evidence.stat().st_mtime < made:
            raise SystemExit(f"refused: {evidence.name} is older than {name} v{ver}: run the evaluation on the candidate first")
        c.patch(f"{LANGFUSE}/{name}/versions/{ver}", json={"newLabels": ["production"]}).raise_for_status()
    rec = {
        "prompt": name,
        "version": ver,
        "by": by.strip(),
        "evidence": str(evidence.relative_to(ROOT)) if evidence.is_relative_to(ROOT) else str(evidence),
    }
    audit_admin("prompt promoted to production", by.strip(), rec)
    return rec


def check() -> dict[str, str]:
    """name -> "ok" | why not. Code must equal Langfuse's production version."""
    out = {}
    with client() as c:
        for name, text in texts().items():
            prod = version(c, name, "production")
            out[name] = ("ok" if prod and prod["prompt"] == text
                         else "no production version: run `python -m agent.ops.prompts push`" if not prod
                         else f"drift: the code differs from production v{prod['version']}: push, evaluate, promote")  # fmt: skip
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["push", "promote", "check"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("version", nargs="?", type=int)
    ap.add_argument("--by", default="")
    ap.add_argument("--evidence", type=Path)
    a = ap.parse_args()
    if a.cmd == "push":
        print(json.dumps(push(), indent=1))
    elif a.cmd == "check":
        res = check()
        print(json.dumps(res, indent=1))
        raise SystemExit(0 if all(v == "ok" for v in res.values()) else 1)
    else:
        if not (a.name and a.version and a.evidence):
            raise SystemExit("promote <name> <version> --by <reviewer> --evidence <eval report>")
        print(json.dumps(promote(a.name, a.version, a.by, a.evidence.resolve())))


if __name__ == "__main__":
    main()
