"""Read the file share into the read model (S2: W2, W5, W6), with the read-only `agent-ro` account.

- every Customers\\<name>\\Quote_<date>.xlsx: the key cells, and whether it exists only in Excel (no Odoo number);
- the calculator's "Ramesh notes" sheet (the input for rules.py);
- the calculator's material price table, compared with Odoo: where they differ, ODOO WINS and the
  conflict is logged in `price_conflicts` (the agent never prices from the share).
Idempotent: rows are replaced by their natural key (path, note number, material code).
"""

from typing import Any

import psycopg

from apex_agent.knowledge.normalize import MaterialMatcher
from apex_agent.mcp_servers.share import CALCULATOR, list_quote_files, quote_fields, read_book


def sync(conn: psycopg.Connection[Any], matcher: MaterialMatcher) -> dict[str, int]:
    quotes = [quote_fields(p) for p in list_quote_files()]
    calc = read_book(CALCULATOR)
    notes = [str(r[1].value) for r in calc["Ramesh notes"].iter_rows(min_row=2) if r[1].value]
    share_prices = {str(r[1].value): float(str(r[3].value)) for r in calc["Materials"].iter_rows(min_row=2) if r[1].value}
    with conn.cursor() as cur:
        # The share is listed in full each run, so a quote file that is gone from the share leaves the read model too.
        cur.execute("DELETE FROM share_quotes WHERE NOT (path = ANY(%s))", ([q["path"] for q in quotes],))
        for q in quotes:
            code = matcher.match(str(q["material"] or ""))[0]
            cur.execute(
                """INSERT INTO share_quotes (path, customer, drawing, material_text, material_code, process, qty, tier,
                       quoted_unit_price, note, odoo_quote, excel_only)
                   VALUES (%(path)s, %(customer)s, %(drawing)s, %(material)s, %(code)s, %(process)s, %(qty)s, %(tier)s,
                       %(quoted_unit_price)s, %(note)s, %(odoo_quote)s, %(excel_only)s)
                   ON CONFLICT (path) DO UPDATE SET customer = EXCLUDED.customer, drawing = EXCLUDED.drawing,
                       material_text = EXCLUDED.material_text, material_code = EXCLUDED.material_code,
                       process = EXCLUDED.process, qty = EXCLUDED.qty, tier = EXCLUDED.tier,
                       quoted_unit_price = EXCLUDED.quoted_unit_price, note = EXCLUDED.note,
                       odoo_quote = EXCLUDED.odoo_quote, excel_only = EXCLUDED.excel_only""",
                {**q, "code": code, "excel_only": not q["odoo_quote"]},
            )
        for n, text in enumerate(notes, 1):
            cur.execute("""INSERT INTO ramesh_notes (n, text) VALUES (%s, %s)
                           ON CONFLICT (n) DO UPDATE SET text = EXCLUDED.text""", (n, text))  # fmt: skip
        cur.execute("""SELECT data->>'default_code', (data->>'standard_price')::numeric FROM odoo_records
                       WHERE model = 'product.template' AND data->>'default_code' LIKE 'MAT-%%'""")  # fmt: skip
        odoo_prices = {code: float(price) for code, price in cur.fetchall()}
        conflicts = 0
        for code, share in share_prices.items():
            odoo = odoo_prices.get(code)
            if odoo and abs(share - odoo) / odoo > 0.005:
                conflicts += 1
                cur.execute(
                    """INSERT INTO price_conflicts (material_code, share_price, odoo_price, diff_pct)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (material_code) DO UPDATE SET share_price = EXCLUDED.share_price,
                           odoo_price = EXCLUDED.odoo_price, diff_pct = EXCLUDED.diff_pct, logged_at = now()""",
                    (code, share, odoo, round(100 * (share - odoo) / odoo, 1)),
                )
        cur.execute("""INSERT INTO sync_state (source, watermark, last_run, seen_last, changed_last)
                       VALUES ('share', now()::text, now(), %s, NULL)
                       ON CONFLICT (source) DO UPDATE SET watermark = EXCLUDED.watermark, last_run = now(),
                           seen_last = EXCLUDED.seen_last""", (len(quotes),))  # fmt: skip
    conn.commit()
    return {"quotes": len(quotes), "excel_only": sum(1 for q in quotes if not q["odoo_quote"]), "notes": len(notes),
            "price_conflicts": conflicts}  # fmt: skip
