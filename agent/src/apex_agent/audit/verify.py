"""verify_audit_chain (S9): walk the audit log in order and recompute every link.

A row is intact when its prev_hash is the previous row's hash and its hash is sha256(prev_hash + payload), where the
payload is recomputed from the stored row by the same SQL function the trigger used. Any edit to a row breaks its
hash; deleting or reordering rows breaks the next link. Prints the head hash (anchor it somewhere else to detect a
rewrite of the whole chain).

Usage: python -m apex_agent.audit.verify       (exit code 1 when the chain is broken)
"""

import hashlib
import json
import sys

import psycopg

from apex_agent.settings import DbSettings, load_as

GENESIS = "0" * 64


def verify(conn: psycopg.Connection[tuple[str, ...]]) -> dict[str, object]:
    prev, n = GENESIS, 0
    with conn.cursor(name="audit_walk") as cur:  # server-side cursor: the log can be long
        cur.execute("SELECT a.id, a.prev_hash, a.hash, audit_payload(a) FROM audit_log a ORDER BY a.id")
        for row_id, prev_hash, stored, payload in cur:
            n += 1
            if prev_hash != prev:
                return {
                    "ok": False,
                    "rows": n,
                    "broken_at": row_id,
                    "why": "prev_hash is not the previous row's hash (a row was removed or inserted)",
                }
            if hashlib.sha256((prev_hash + payload).encode()).hexdigest() != stored:
                return {
                    "ok": False,
                    "rows": n,
                    "broken_at": row_id,
                    "why": "the row's content no longer matches its hash (it was edited)",
                }
            prev = stored
    return {"ok": True, "rows": n, "head": prev}


if __name__ == "__main__":
    with psycopg.connect(load_as(DbSettings).url.get_secret_value()) as c:
        out = verify(c)
    print(json.dumps(out))
    sys.exit(0 if out["ok"] else 1)
