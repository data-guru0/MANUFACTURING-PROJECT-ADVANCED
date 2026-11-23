"""Quote terms (S5, W8): a validity date and a material price-protection clause tied to a REAL public index.

- validity: policy days; shorter when the material's index moved more than the policy % in the last 6 months,
  or when the material has no public index at all (titanium, POM).
- price protection: the World Bank Pink Sheet value of the material's series on the quote date is stored with the
  quote; the clause adjusts only the MATERIAL portion of the price if the index moves more than X % before acceptance.
"""

from datetime import date, timedelta
from typing import Any

import psycopg


def index_on(conn: psycopg.Connection[Any], series: str, on: date) -> tuple[str, float, str] | None:
    """(month, value, unit) of the latest monthly value at or before `on`."""
    with conn.cursor() as cur:
        cur.execute("SELECT month, value, unit FROM commodity_index WHERE series = %s AND month <= %s ORDER BY month DESC LIMIT 1",
                    (series, on))  # fmt: skip
        row = cur.fetchone()
    return (row[0].isoformat()[:7], float(row[1]), str(row[2])) if row else None


def terms(conn: psycopg.Connection[Any], material: str, material_usd_per_part: float, quote_date: date,
          policy: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    series = (policy.get("index_for_material") or {}).get(material)
    days = policy["validity_days"]
    if not series:
        return {"validity_date": (quote_date + timedelta(days=int(days["volatile"]))).isoformat(), "volatile": True,
                "index": None, "clause": None,
                "why": f"no public monthly index for {material}: the short validity of {days['volatile']} days applies"}  # fmt: skip
    now = index_on(conn, series, quote_date)
    then = index_on(conn, series, quote_date - timedelta(days=183))
    if now is None:
        return {"validity_date": (quote_date + timedelta(days=int(days["volatile"]))).isoformat(), "volatile": True, "index": None,
                "clause": None, "why": f"the {series} index has not been fetched (make terms-index): short validity"}  # fmt: skip
    move = abs(now[1] / then[1] - 1) * 100 if then else 0.0
    volatile = move > float(policy["volatile_move_pct"])
    validity = quote_date + timedelta(days=int(days["volatile" if volatile else "default"]))
    x = float(policy["price_protection_pct"])
    clause = (f"Material price protection: this quote uses the World Bank Pink Sheet {series} price of {now[1]:,.2f} {now[2]} "
              f"({now[0]}). If that index moves more than {x:g} % before acceptance, the material portion of the price "
              f"(${material_usd_per_part:.2f} per part) is adjusted in proportion.")  # fmt: skip
    return {"validity_date": validity.isoformat(), "volatile": volatile, "index": {"series": series, "month": now[0],
            "value": now[1], "unit": now[2], "six_month_move_pct": round(move, 1)}, "clause": clause,
            "why": f"{series} moved {move:.1f} % in 6 months ({'>' if volatile else '<='} {policy['volatile_move_pct']} %)"}  # fmt: skip
