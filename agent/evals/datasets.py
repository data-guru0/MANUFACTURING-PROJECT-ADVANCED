"""Langfuse datasets (S10): the evaluation data, kept where the traces and prompt versions are.

  apex-triage        the labelled RFQ mix: subject + sender -> expected category, attack type    (data/generated/rfq_labels.jsonl)
  apex-extraction    the RFQ drawings' ground truth: part -> material, size, holes, tolerance... (data/generated/ground_truth/RFQ-*.json)
  apex-human-edits   every correction a reviewer made: field, what the agent had, what the person set, why (`feedback`)

Idempotent: each item has a stable id (Langfuse upserts by id). No LLM call.
Usage: uv run python -m agent.evals.datasets
"""

import hashlib
import json
import ssl
from typing import Any

import httpx

from agent.evals.run_evals import sql
from legacy.odoo.rpc import ROOT, env

API = "https://localhost:8443/api/public"


def item_id(dataset: str, key: str) -> str:
    return hashlib.sha256(f"{dataset}:{key}".encode()).hexdigest()[:32]


def main() -> None:
    tls = ssl.create_default_context(cafile=str(ROOT / "data" / "generated" / "certs" / "ca.crt"))
    c = httpx.Client(auth=(env("LANGFUSE_PUBLIC_KEY"), env("LANGFUSE_SECRET_KEY")), verify=tls, timeout=30)
    gen = ROOT / "data" / "generated"
    labels = {
        lab["message_id"]: lab
        for lab in (json.loads(x) for x in (gen / "rfq_labels.jsonl").read_text(encoding="utf-8").splitlines() if x.strip())
    }
    truth = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((gen / "ground_truth").glob("RFQ-*.json"))]
    edits = [json.loads(r) for r in sql("""SELECT json_build_object('id', id, 'email', email_id, 'field', field, 'old', old_value,
                                              'new', new_value, 'by', by_user, 'reason', reason) FROM feedback ORDER BY id""").splitlines() if r]  # fmt: skip
    sets: dict[str, tuple[str, list[tuple[str, dict[str, Any], Any, dict[str, Any]]]]] = {
        "apex-triage": ("Labelled RFQ traffic (L5): expected triage category and attack type", [
            (mid, {"subject": lab["subject"], "from": lab["from"], "language": lab.get("language")},
             {"category": "rfq" if lab["category"] == "rfq" else "not_rfq", "attack": lab["attack"]}, {"source": "rfq_labels.jsonl"})
            for mid, lab in labels.items()]),
        "apex-extraction": ("RFQ drawings' ground truth (L3): what extraction must read", [
            (g["part_code"], {"part_code": g["part_code"], "files": g.get("files"), "scan": g.get("scan")},
             {k: g.get(k) for k in ("material", "envelope_mm", "holes", "threads", "tightest_tolerance_mm", "general_tolerance", "ra_um")},
             {"source": "ground_truth", "kind": g.get("kind")}) for g in truth]),
        "apex-human-edits": ("Reviewers' corrections (S7+): the agent's value, the person's value, and why", [
            (str(e["id"]), {"email_id": e["email"], "field": e["field"], "agent_value": e["old"]}, {"value": e["new"]},
             {"by": e["by"], "reason": e["reason"]}) for e in edits]),
    }  # fmt: skip
    out = {}
    for name, (description, items) in sets.items():
        c.post(f"{API}/v2/datasets", json={"name": name, "description": description}).raise_for_status()
        for key, inp, expected, meta in items:
            c.post(f"{API}/dataset-items", json={"id": item_id(name, key), "datasetName": name, "input": inp,
                                                 "expectedOutput": expected, "metadata": meta}).raise_for_status()  # fmt: skip
        out[name] = len(items)
    print(json.dumps({"datasets": out}))


if __name__ == "__main__":
    main()
