"""Incremental, idempotent copy of the Odoo records the agent needs into the read model (S2, W2).

- logs in as `erp_reader` (read-only; never the shared `office` login);
- per model, reads only records with write_date >= the last watermark;
- upserts into `odoo_records` and counts a row as changed only if its data really differs, so a second
  run with nothing new in Odoo reports 0 changed.
ponytail: deletions in Odoo are not propagated (the legacy data is never deleted); add a periodic full
id reconciliation if that changes.
"""

import json
from typing import Any

import psycopg

from apex_agent.odoo import Odoo
from apex_agent.settings import OdooSettings, load_as

MODELS: dict[str, list[str]] = {
    "product.template": ["default_code", "name", "x_part_family", "x_material_grade", "x_drawing_no", "bom_ids",
                         "standard_price", "categ_id"],
    "product.product": ["default_code", "product_tmpl_id"],
    "mrp.bom": ["product_tmpl_id", "bom_line_ids", "operation_ids"],
    "mrp.bom.line": ["bom_id", "product_id", "product_qty"],
    "mrp.routing.workcenter": ["bom_id", "workcenter_id", "name", "time_cycle_manual"],
    "mrp.workcenter": ["code", "name", "costs_hour", "time_start", "x_max_x_mm", "x_max_y_mm", "x_max_z_mm",
                       "x_max_weight_kg", "x_best_tolerance_mm", "x_best_ra", "x_power_kw"],
    "res.partner": ["ref", "name", "email", "customer_rank", "supplier_rank", "x_nda_restricted", "x_required_certs",
                    "x_language"],
    "sale.order": ["name", "client_order_ref", "partner_id", "state", "date_order", "amount_untaxed", "validity_date",
                   "x_rfq_received_at", "x_quote_sent_at", "x_lost_reason", "commitment_date", "effective_date"],
    "sale.order.line": ["order_id", "product_id", "product_uom_qty", "price_unit"],
    "mrp.production": ["name", "origin", "product_id", "state", "product_qty", "date_finished"],
    "mrp.workorder": ["production_id", "workcenter_id", "duration_expected", "duration"],
    "purchase.order": ["name", "origin", "state", "amount_untaxed", "partner_id"],  # S18: bought material
    "product.supplierinfo": ["partner_id", "product_tmpl_id", "price", "delay", "date_start"],
    "stock.quant": ["product_id", "location_id", "quantity"],
}  # fmt: skip
PAGE = 500


def sync(conn: psycopg.Connection[Any], odoo: Odoo | None = None) -> dict[str, tuple[int, int]]:
    """Returns {model: (records read, records changed)}."""
    odoo = odoo or Odoo(load_as(OdooSettings))
    out = {}
    for model, fields in MODELS.items():
        with conn.cursor() as cur:
            cur.execute("SELECT watermark FROM sync_state WHERE source = %s", (f"odoo:{model}",))
            row = cur.fetchone()
        domain = [("write_date", ">=", row[0])] if row and row[0] else []
        seen = changed = 0
        mark = row[0] if row else None
        offset = 0
        while True:
            recs = odoo.call(model, "search_read", domain, fields=[*fields, "write_date"], order="write_date, id",
                             limit=PAGE, offset=offset)  # fmt: skip
            if not recs:
                break
            with conn.cursor() as cur:
                for r in recs:
                    data = json.dumps({k: r[k] for k in fields}, sort_keys=True, default=str)
                    cur.execute(
                        """INSERT INTO odoo_records (model, id, write_date, data) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (model, id) DO UPDATE SET write_date = EXCLUDED.write_date, data = EXCLUDED.data,
                               synced_at = now()
                           WHERE odoo_records.data IS DISTINCT FROM EXCLUDED.data""",
                        (model, r["id"], r["write_date"], data),
                    )
                    changed += cur.rowcount
                    mark = max(mark or r["write_date"], r["write_date"])
            seen += len(recs)
            offset += PAGE
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sync_state (source, watermark, last_run, seen_last, changed_last)
                   VALUES (%s, %s, now(), %s, %s)
                   ON CONFLICT (source) DO UPDATE SET watermark = EXCLUDED.watermark, last_run = now(),
                       seen_last = EXCLUDED.seen_last, changed_last = EXCLUDED.changed_last""",
                (f"odoo:{model}", mark, seen, changed),
            )
        conn.commit()
        out[model] = (seen, changed)
    return out
