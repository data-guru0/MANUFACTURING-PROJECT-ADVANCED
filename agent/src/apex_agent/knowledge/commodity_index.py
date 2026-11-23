"""Fetch the World Bank Pink Sheet (monthly commodity prices) for real and store the series Apex needs (S5, W8).

The download goes out ONLY through the Envoy egress allow-list (HTTPS_PROXY=envoy-egress), which now allows
thedocs.worldbank.org. The source URL and the series come from agent/config/costing.yaml -> commodity_index.

Usage: python -m apex_agent.knowledge.commodity_index    -> {"series": 5, "months": ..., "latest": "2026-10", ...}
"""

import io
import json
import re
from datetime import date
from typing import Any

import httpx
import openpyxl

from apex_agent.costing.standards import shop
from apex_agent.knowledge import db


def fetch(url: str) -> bytes:
    # trust_env: HTTPS_PROXY is the egress gateway; a host not on its allow-list gets a 403 CONNECT
    r = httpx.get(url, timeout=120, follow_redirects=True, trust_env=True, headers={"User-Agent": "apex-fde-agent/0.1"})
    r.raise_for_status()
    return r.content


def parse(data: bytes, sheet: str, wanted: list[str]) -> list[tuple[str, date, float, str]]:
    ws = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)[sheet]
    rows = list(ws.iter_rows(values_only=True))
    hdr = next(i for i, r in enumerate(rows) if r and any(str(c or "").startswith("Crude oil") for c in r))
    names, units = rows[hdr], rows[hdr + 1]
    cols = {str(n).strip(): i for i, n in enumerate(names) if n}
    out = []
    for name in wanted:
        i = cols[name]
        unit = str(units[i] or "").strip("()")
        for r in rows[hdr + 2 :]:
            m = re.match(r"^(\d{4})M(\d{2})$", str(r[0] or ""))
            if m and isinstance(r[i], int | float):
                out.append((name, date(int(m[1]), int(m[2]), 1), float(r[i]), unit))  # type: ignore[arg-type]
    return out


def main() -> dict[str, Any]:
    cfg = shop()["commodity_index"]
    values = parse(fetch(cfg["url"]), cfg["sheet"], cfg["series"])
    conn = db.connect()
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO commodity_index (series, month, value, unit, source_url) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (series, month) DO UPDATE SET value = EXCLUDED.value, unit = EXCLUDED.unit,
                 source_url = EXCLUDED.source_url, fetched_at = now()""",
            [(s, m, v, u, cfg["url"]) for s, m, v, u in values],
        )
    conn.commit()
    latest = max(m for _, m, _, _ in values)
    summary = {"series": len(cfg["series"]), "months": len(values), "latest": latest.isoformat()[:7],
               "latest_values": {s: v for s, m, v, _ in values if m == latest}, "source": cfg["url"]}  # fmt: skip
    print(json.dumps(summary, default=str))
    return summary


if __name__ == "__main__":
    main()
