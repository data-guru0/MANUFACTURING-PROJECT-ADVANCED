"""Put the human-approved pricing rules back to the engagement's start (part of `make demo-reset`).

`policy.yaml → pricing_rules` and `rule_proposals.yaml` are written by people during the course (S2: R-001/R-002 from
Ramesh's notes, S18: R-003 mined from overrides). `demo-reset` wipes the data those decisions were made on, so it puts
the rules back to "none approved, nothing proposed" too; otherwise the next run would number them R-004… and price
7075 with S18's rule from day one. Everything above the marker (the reviewed policy) is left untouched.
"""

from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "agent" / "config"
MARKER = "# --- pricing_rules: managed by `python -m apex_agent.knowledge.rules approve` (human-approved only) ---"
HEADER = "# Proposed by the LLM; NOT applied. Approve with: python -m apex_agent.knowledge.rules approve <id> --by <name>\n"


def main() -> None:
    policy = CONFIG / "policy.yaml"
    text = policy.read_text(encoding="utf-8")
    head, found, _ = text.partition(MARKER)
    if not found:
        raise SystemExit(f"{policy}: marker not found, nothing changed")
    policy.write_text(head + MARKER + "\npricing_rules: []\n", encoding="utf-8", newline="\n")
    (CONFIG / "rule_proposals.yaml").write_text(HEADER + "proposals: []\n", encoding="utf-8", newline="\n")
    print("pricing rules: none approved, nothing proposed (policy.yaml, rule_proposals.yaml)")


if __name__ == "__main__":
    main()
