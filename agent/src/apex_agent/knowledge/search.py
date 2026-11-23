"""Hybrid search for analog parts (S2, W2/W10): structured filters + Postgres full-text + pgvector.
Each hit carries what an estimator needs from history: won and lost prices, quotes, actual minutes per unit.

score = 0.7 * cosine similarity (local embedding) + 0.3 * full-text rank (normalized), after the filters.
ponytail: fixed weights; tune on the S10 retrieval eval if analogs disappoint.

Usage: python -m apex_agent.knowledge.search "flanged housing stainless 304" [--material MAT-SS304] [--family x]
"""

import argparse
import json
from typing import Any

import psycopg

from apex_agent.knowledge.db import connect
from apex_agent.knowledge.embed import embed_query

FIELDS = ["code", "name", "family", "material_code", "customer_ref", "quotes", "won", "last_price", "won_prices",
          "lost_prices", "actual_min_per_unit", "routing_source", "source"]  # fmt: skip


def search(conn: psycopg.Connection[Any], text: str, *, k: int = 5, material: str | None = None,
           family: str | None = None, exclude: str | None = None) -> list[dict[str, Any]]:  # fmt: skip
    vec = "[" + ",".join(f"{x:.6f}" for x in embed_query(text)) + "]"
    where, params = ["embedding IS NOT NULL"], []
    for col, val in (("material_code", material), ("family", family)):
        if val:
            where.append(f"{col} = %s")
            params.append(val)
    if exclude:
        where.append("code <> %s")
        params.append(exclude)
    sql = f"""SELECT {", ".join(FIELDS)}, 1 - (embedding <=> %s::vector) AS vec,
                     ts_rank(tsv, replace(plainto_tsquery('english', %s)::text, '&', '|')::tsquery) AS fts
              FROM parts WHERE {" AND ".join(where)}"""  # nosec B608 (column names are constants; values are parameters)
    with conn.cursor() as cur:
        cur.execute(sql, [vec, text, *params])
        rows = [dict(zip([*FIELDS, "vec", "fts"], r, strict=True)) for r in cur.fetchall()]
    top_fts = max((float(r["fts"]) for r in rows), default=0.0) or 1.0
    for r in rows:
        r["score"] = round(0.7 * float(r["vec"]) + 0.3 * float(r["fts"]) / top_fts, 4)
    return sorted(rows, key=lambda r: r["score"], reverse=True)[:k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--material")
    ap.add_argument("--family")
    ap.add_argument("-k", type=int, default=5)
    a = ap.parse_args()
    with connect() as conn:
        for r in search(conn, a.text, k=a.k, material=a.material, family=a.family):
            print(json.dumps({k: r[k] for k in ("code", "family", "material_code", "quotes", "won", "won_prices",
                                                "lost_prices", "actual_min_per_unit", "score")}, default=str))  # fmt: skip


if __name__ == "__main__":
    main()
