"""The knowledge sync job (S2): Odoo -> share -> normalize -> parts + embeddings. Idempotent; run it again
any time. Prints one JSON summary line (records read/changed per model, share counts, findings).

Usage (inside the `knowledge` container): python -m apex_agent.knowledge.sync
"""

import json

from apex_agent.knowledge import normalize, sync_odoo, sync_share
from apex_agent.knowledge.db import connect


def main() -> None:
    matcher = normalize.MaterialMatcher()
    with connect() as conn:
        odoo = sync_odoo.sync(conn)
        share = sync_share.sync(conn, matcher)
        dups = normalize.flag_duplicates(conn)
        parts = normalize.build_parts(conn, matcher)
        conn.commit()
    print(json.dumps({"odoo_read": sum(s for s, _ in odoo.values()), "odoo_changed": sum(c for _, c in odoo.values()),
                      "per_model_changed": {m: c for m, (_, c) in odoo.items()}, "share": share,
                      "duplicate_customers": dups, **parts}))  # fmt: skip


if __name__ == "__main__":
    main()
