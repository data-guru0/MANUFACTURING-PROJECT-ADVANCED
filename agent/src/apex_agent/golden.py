"""The live golden check (S10): re-run triage, exactly as the worker calls it, on a fixed set of labelled emails, with
the prompt in the code NOW, on 3 labelled emails (small on purpose: the free-tier quota). Nothing is written back. Identical calls come from LiteLLM's Redis cache (no tokens), so an
unchanged prompt costs nothing; a changed prompt misses the cache, gets real answers, and a broken one scores lower.

Usage (worker container): python -m apex_agent.golden < golden.json      # [{"message_id": ..., "expected": "rfq"}, ...]
Prints {"golden_triage_accuracy": ..., "n": ..., "misses": [...]}.
"""

import json
import sys
import uuid

from apex_agent.llm.client import QuotaExhausted
from apex_agent.triage import triage
from apex_agent.worker import Worker


def main() -> None:
    cases = json.load(sys.stdin)
    w = Worker()
    hits, misses = 0, []
    with w.conn.cursor() as cur:
        for case in cases:
            cur.execute("SELECT id FROM emails WHERE message_id = %s", (case["message_id"],))
            row = cur.fetchone()
            if not row:
                continue
            facts, text, _ = w.triage_inputs(row[0])
            try:
                got = triage(w.triage_llm, facts, text, uuid.uuid4().hex)["category"]
            except QuotaExhausted:  # not measured today (Groq's daily quota): say so, never guess a score
                print(json.dumps({"golden_triage_accuracy": None, "n": 0, "skipped": "Groq daily quota for quote-triage"}))
                return
            got = "rfq" if got == "rfq" else "not_rfq"
            hits += got == case["expected"]
            if got != case["expected"]:
                misses.append({"email_id": row[0], "expected": case["expected"], "got": got})
    n = hits + len(misses)
    print(json.dumps({"golden_triage_accuracy": round(hits / n, 3) if n else None, "n": n, "misses": misses}))


if __name__ == "__main__":
    main()
