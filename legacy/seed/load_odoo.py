"""L2: load the generated history into Odoo through JSON-RPC, like a data migration would.

Idempotent and resumable:
- every record has a natural key (partner `ref`, product `default_code`, SO `client_order_ref`,
  PO `partner_ref`, MO `origin`) and is looked up before it is created;
- a quote is marked `x_seed_batch = "L2-history"` only after ALL of its steps are done
  (until then "L2-partial"), so a crashed run resumes where it stopped.

Run: uv run python -m legacy.seed.load_odoo   (needs L1: python -m legacy.odoo.setup_odoo)
"""

import time
from datetime import datetime, timedelta
from typing import Any

from legacy.odoo.rpc import Odoo, env
from legacy.seed.history import Customer, History, PartRec, Quote, generate

SEED = "L2-history"
PARTIAL = "L2-partial"
FMT = "%Y-%m-%d %H:%M:%S"
NO_WARN = {"disable_cancel_warning": True, "tracking_disable": True}


def ts(d: datetime) -> str:
    return d.strftime(FMT)


class Loader:
    def __init__(self, odoo: Odoo, h: History) -> None:
        self.o, self.h = odoo, h
        self.partner: dict[str, int] = {}
        self.tmpl: dict[str, int] = {}
        self.product: dict[str, int] = {}
        self.wc = {w["code"]: w["id"] for w in odoo.call("mrp.workcenter", "search_read", [], fields=["code"])}

    # ------------------------------------------------------------ master data
    def partners(self) -> None:
        o = self.o
        tiers = {t: o.ensure("res.partner.category", [("name", "=", f"Tier {t}")], {"name": f"Tier {t}"})
                 for t in "ABC"}  # fmt: skip
        existing = {p["ref"]: p["id"] for p in o.call("res.partner", "search_read",
                    [("x_seed_batch", "=", SEED)], fields=["ref"])}  # fmt: skip
        countries = {c["code"]: c["id"] for c in o.call("res.country", "search_read",
                     [("code", "in", ["US", "DE", "ES", "MX", "IN"])], fields=["code"])}  # fmt: skip
        new: list[dict[str, Any]] = []
        for c in self.h.customers:
            if c.key not in existing:
                new.append(self._customer_vals(c, tiers, countries))
        for s in self.h.suppliers:
            if s.key not in existing:
                new.append({"name": s.name, "ref": s.key, "is_company": True, "email": s.email,
                            "supplier_rank": 1, "country_id": countries["US"], "x_seed_batch": SEED,
                            "comment": f"Supplier type: {s.kind}"})  # fmt: skip
        if new:
            o.call("res.partner", "create", new)
        self.partner = {p["ref"]: p["id"] for p in o.call("res.partner", "search_read",
                        [("x_seed_batch", "=", SEED)], fields=["ref"])}  # fmt: skip
        print(f"partners: {len(self.h.customers)} customer records, {len(self.h.suppliers)} suppliers"
              f" ({len(new)} created)")  # fmt: skip

    @staticmethod
    def _customer_vals(c: Customer, tiers: dict[str, int], countries: dict[str, int]) -> dict[str, Any]:
        return {"name": c.name, "ref": c.key, "is_company": True, "email": c.email, "customer_rank": 1,
                "country_id": countries[c.country], "category_id": [(6, 0, [tiers[c.tier]])],
                "x_nda_restricted": c.nda, "x_required_certs": c.certs, "x_language": c.language,
                "x_seed_batch": SEED}  # fmt: skip

    def products(self) -> None:
        o = self.o
        existing = {p["default_code"] for p in o.call("product.template", "search_read",
                    [("x_seed_batch", "=", SEED)], fields=["default_code"])}  # fmt: skip
        categ = o.ensure("product.category", [("name", "=", "Customer Parts")], {"name": "Customer Parts"})
        new = [{
            "name": f"{p.label} {p.drawing} Rev {p.rev}", "default_code": p.code, "detailed_type": "product",
            "categ_id": categ, "sale_ok": True, "purchase_ok": True, "x_drawing_no": p.drawing,
            "x_material_grade": p.grade_text, "x_part_family": p.family, "x_seed_batch": SEED,
        } for p in self.h.parts.values() if p.code not in existing]  # fmt: skip
        for i in range(0, len(new), 100):
            o.call("product.template", "create", new[i : i + 100])
        rows = o.call("product.product", "search_read", [("product_tmpl_id.x_seed_batch", "=", SEED)],
                      fields=["default_code", "product_tmpl_id"])  # fmt: skip
        self.product = {r["default_code"]: r["id"] for r in rows}
        self.tmpl = {r["default_code"]: r["product_tmpl_id"][0] for r in rows}
        mats = o.call("product.product", "search_read", [("default_code", "=like", "MAT-%")],
                      fields=["default_code", "product_tmpl_id"])  # fmt: skip
        self.product.update({r["default_code"]: r["id"] for r in mats})
        self.tmpl.update({r["default_code"]: r["product_tmpl_id"][0] for r in mats})
        print(f"parts: {len(self.h.parts)} ({len(new)} created)")

    def boms(self) -> None:
        o = self.o
        have = {b["product_tmpl_id"][0] for b in o.call("mrp.bom", "search_read",
                [("product_tmpl_id.x_seed_batch", "=", SEED)], fields=["product_tmpl_id"])}  # fmt: skip
        new = [self._bom_vals(p) for p in self.h.parts.values() if self.tmpl[p.code] not in have]
        for i in range(0, len(new), 50):
            o.call("mrp.bom", "create", new[i : i + 50])
        no_routing = sum(1 for p in self.h.parts.values() if not p.has_routing)
        print(f"BOMs: {len(self.h.parts)} ({len(new)} created), {no_routing} without routing (W4)")

    def _bom_vals(self, p: PartRec) -> dict[str, Any]:
        ops = [(0, 0, {"name": wc, "workcenter_id": self.wc[wc], "time_mode": "manual",
                       "time_cycle_manual": p.cycle_min[wc], "sequence": 10 * (i + 1)})
               for i, wc in enumerate(p.spec.routing)] if p.has_routing else []  # fmt: skip
        return {"product_tmpl_id": self.tmpl[p.code], "product_qty": 1, "consumption": "flexible",
                "bom_line_ids": [(0, 0, {"product_id": self.product[p.spec.material],
                                         "product_qty": p.stock_kg})],
                "operation_ids": ops}  # fmt: skip

    def supplier_prices(self) -> None:
        o = self.o
        have = {(r["partner_id"][0], r["product_tmpl_id"][0]) for r in o.call("product.supplierinfo",
                "search_read", [("partner_id.x_seed_batch", "=", SEED)], fields=["partner_id", "product_tmpl_id"])}  # fmt: skip
        osp = {r["default_code"]: r["id"] for r in o.call("product.template", "search_read",
               [("default_code", "=like", "OSP-%")], fields=["default_code"])}  # fmt: skip
        new = []
        for s in self.h.suppliers:
            for code, price, lead, valid_from in s.pricelist:
                t = self.tmpl.get(code) or osp[code]
                if (self.partner[s.key], t) not in have:
                    new.append({"partner_id": self.partner[s.key], "product_tmpl_id": t, "price": price,
                                "delay": lead, "date_start": valid_from.isoformat()})  # fmt: skip
        if new:
            o.call("product.supplierinfo", "create", new)
        print(f"supplier price-list lines: {len(new)} created (W13: valid-from dates up to 2 years old)")

    def stock_on_hand(self) -> None:
        o = self.o
        loc = o.call("stock.warehouse", "search_read", [], fields=["lot_stock_id"], limit=1)[0]["lot_stock_id"][0]
        quants = o.call("stock.quant", "search_read", [("product_id.default_code", "=like", "MAT-%"),
                        ("location_id", "=", loc)], fields=["id"])  # fmt: skip
        if quants:
            print("stock on hand: already set")
            return
        ids = [o.call("stock.quant", "create", {"product_id": self.product[m], "location_id": loc,
                                                "inventory_quantity": kg})
               for m, kg in self.h.stock_kg.items()]  # fmt: skip
        o.call("stock.quant", "action_apply_inventory", ids)
        print(f"stock on hand: {len(ids)} materials")

    # ------------------------------------------------------------ transactions
    def quotes(self) -> None:
        o = self.o
        rows = o.call("sale.order", "search_read", [("client_order_ref", "=like", "HIST-%")],
                      fields=["client_order_ref", "x_seed_batch"])  # fmt: skip
        done = {r["client_order_ref"] for r in rows if r["x_seed_batch"] == SEED}
        todo = [q for q in self.h.quotes if q.ref not in done]
        t0 = time.monotonic()
        for i, q in enumerate(todo, 1):
            self._quote(q)
            if i % 50 == 0:
                print(f"  quotes: {i}/{len(todo)} ({time.monotonic() - t0:.0f}s)")
        # W3: every quote was typed in under the one shared login, so that's the "salesperson".
        office = o.call("res.users", "search", [("login", "=", "office")], limit=1)[0]
        wrong = o.call("sale.order", "search", [("client_order_ref", "=like", "HIST-%"), ("user_id", "!=", office)])
        if wrong:
            o.call("sale.order", "write", wrong, {"user_id": office})
        print(f"quotes: {len(self.h.quotes)} ({len(todo)} loaded now), salesperson = Office (shared)")

    def _quote(self, q: Quote) -> None:
        o = self.o
        so_rows = o.call("sale.order", "search_read", [("client_order_ref", "=", q.ref)],
                         fields=["name", "state"])  # fmt: skip
        if so_rows:
            so, name, state = so_rows[0]["id"], so_rows[0]["name"], so_rows[0]["state"]
        else:
            so = o.call("sale.order", "create", {
                "partner_id": self.partner[q.partner_key], "client_order_ref": q.ref,
                "date_order": ts(q.sent_at), "x_rfq_received_at": ts(q.received_at),
                "x_quote_sent_at": ts(q.sent_at), "x_seed_batch": PARTIAL,
                # Odoo's default expiry is "30 days from today"; for history it must be 30 days from the quote.
                "validity_date": (q.sent_at + timedelta(days=30)).strftime("%Y-%m-%d"),
                "order_line": [(0, 0, {"product_id": self.product[q.part_code], "product_uom_qty": q.qty,
                                       "price_unit": q.unit_price})],
            })  # fmt: skip
            if q.note:
                o.call("sale.order", "message_post", [so], body=q.note)
            o.call("sale.order", "action_quotation_sent", [so])
            name = o.call("sale.order", "read", [so], ["name"])[0]["name"]
            state = "sent"

        if q.outcome == "lost" and state != "cancel":
            o.call("sale.order", "action_cancel", [so], context=NO_WARN)
            if q.lost_reason:
                o.call("sale.order", "write", [so], {"x_lost_reason": q.lost_reason})
        elif q.outcome == "won":
            assert q.job is not None
            if state in ("draft", "sent"):
                o.call("sale.order", "action_confirm", [so])
            o.call("sale.order", "write", [so], {"date_order": ts(q.job.confirm_at),
                                                 "commitment_date": ts(q.job.commitment_at)})  # fmt: skip
            self._job(q, so, name)
        o.call("sale.order", "write", [so], {"x_seed_batch": SEED})

    def _job(self, q: Quote, so: int, so_name: str) -> None:
        job = q.job
        assert job is not None
        part = self.h.parts[q.part_code]
        if job.material_po:
            sup, kg, ppk, ordered, received = job.material_po
            self._purchase(f"{q.ref}-MAT", sup, part.spec.material, kg, ppk, ordered, received, so_name)
        if job.subcon_po:
            sup, unit, ordered, received = job.subcon_po
            self._purchase(f"{q.ref}-SUB", sup, part.code, q.qty, unit, ordered, received, so_name)
        if job.kind in ("done", "open"):
            self._production(q, so_name)
        if job.delivered_at:
            self._deliver(so, job.delivered_at)

    def _purchase(self, ref: str, sup: str, code: str, qty: float, price: float,
                  ordered: datetime, received: datetime, origin: str) -> None:  # fmt: skip
        o = self.o
        rows = o.call("purchase.order", "search_read", [("partner_ref", "=", ref)], fields=["state"])
        if rows:
            po, state = rows[0]["id"], rows[0]["state"]
        else:
            po = o.call("purchase.order", "create", {
                "partner_id": self.partner[sup], "partner_ref": ref, "origin": origin,
                "date_order": ts(ordered), "x_seed_batch": SEED,
                "order_line": [(0, 0, {"product_id": self.product[code], "product_qty": qty,
                                       "price_unit": price, "date_planned": ts(received)})],
            })  # fmt: skip
            state = "draft"
        if state in ("draft", "sent"):
            o.call("purchase.order", "button_confirm", [po])
            o.call("purchase.order", "write", [po], {"date_order": ts(ordered), "date_approve": ts(ordered)})
        self._validate_pickings([("purchase_id", "=", po)], received)

    def _validate_pickings(self, domain: list[Any], when: datetime) -> None:
        o = self.o
        for p in o.call("stock.picking", "search_read", domain + [("state", "!=", "cancel")],
                        fields=["state"]):  # fmt: skip
            if p["state"] != "done":
                for m in o.call("stock.move", "search_read", [("picking_id", "=", p["id"])],
                                fields=["product_uom_qty"]):  # fmt: skip
                    o.call("stock.move", "write", [m["id"]], {"quantity": m["product_uom_qty"], "picked": True})
                o.call("stock.picking", "button_validate", [p["id"]],
                       context={"skip_backorder": True, "skip_sms": True})  # fmt: skip
            o.call("stock.picking", "write", [p["id"]], {"date_done": ts(when)})

    def _production(self, q: Quote, origin: str) -> None:
        o = self.o
        job, part = q.job, self.h.parts[q.part_code]
        assert job is not None and job.mo_start is not None and job.mo_finish is not None
        rows = o.call("mrp.production", "search_read", [("origin", "=", origin)], fields=["state"])
        if rows:
            mo, state = rows[0]["id"], rows[0]["state"]
        else:
            bom = o.call("mrp.bom", "search", [("product_tmpl_id", "=", self.tmpl[part.code])], limit=1)[0]
            mo = o.call("mrp.production", "create", {
                "product_id": self.product[part.code], "product_qty": q.qty, "bom_id": bom,
                "origin": origin, "date_start": ts(job.mo_start), "x_seed_batch": SEED,
            })  # fmt: skip
            state = "draft"
        if state == "draft":
            o.call("mrp.production", "action_confirm", [mo])
        if job.kind == "open":
            o.call("mrp.production", "button_plan", [mo])
            return
        if state == "done":
            return
        # W14: record ACTUAL minutes per work order and the material really consumed (scrap).
        for wo in o.call("mrp.workorder", "search_read", [("production_id", "=", mo)],
                         fields=["workcenter_id"]):  # fmt: skip
            code = next(c for c, i in self.wc.items() if i == wo["workcenter_id"][0])
            o.call("mrp.workorder", "write", [wo["id"]], {"duration": job.wo_actual_min.get(code, 0.0)})
        o.call("mrp.production", "write", [mo], {"qty_producing": q.qty})
        for m in o.call("stock.move", "search_read", [("raw_material_production_id", "=", mo)],
                        fields=["product_uom_qty"]):  # fmt: skip
            used = round(m["product_uom_qty"] * (1 + job.scrap_pct), 3)
            o.call("stock.move", "write", [m["id"]], {"quantity": used, "picked": True})
        o.call("mrp.production", "button_mark_done", [mo])
        # Odoo won't move a done MO's start date; the work orders carry the real start/finish.
        o.call("mrp.production", "write", [mo], {"date_finished": ts(job.mo_finish)})
        wos = o.call("mrp.workorder", "search", [("production_id", "=", mo)])
        if wos:
            o.call("mrp.workorder", "write", wos, {"date_start": ts(job.mo_start),
                                                   "date_finished": ts(job.mo_finish)})  # fmt: skip

    def _deliver(self, so: int, when: datetime) -> None:
        self._validate_pickings([("sale_id", "=", so)], when)

    def blanket_mos(self) -> None:
        o = self.o
        have = {r["origin"] for r in o.call("mrp.production", "search_read", [("origin", "=like", "BLK-%")],
                                            fields=["origin"])}  # fmt: skip
        names = {c.key: c.name for c in self.h.customers}
        created = 0
        for b in self.h.blanket_mos:
            origin = f"{b.ref} blanket order {names[b.customer_key]}"
            if origin in have:
                continue
            part = self.h.parts[b.part_code]
            bom = o.call("mrp.bom", "search", [("product_tmpl_id", "=", self.tmpl[part.code])], limit=1)[0]
            mo = o.call("mrp.production", "create", {
                "product_id": self.product[part.code], "product_qty": b.qty, "bom_id": bom,
                "origin": origin, "date_start": ts(b.start_at), "x_seed_batch": SEED,
            })  # fmt: skip
            o.call("mrp.production", "action_confirm", [mo])
            o.call("mrp.production", "button_plan", [mo])
            created += 1
        print(f"open blanket manufacturing orders: {len(self.h.blanket_mos)} ({created} created)")


def main() -> None:
    t0 = time.monotonic()
    h = generate()
    odoo = Odoo("admin", env("ODOO_ADMIN_PASSWORD"))
    loader = Loader(odoo, h)
    loader.partners()
    loader.products()
    loader.boms()
    loader.supplier_prices()
    loader.stock_on_hand()
    loader.quotes()
    loader.blanket_mos()
    print(f"L2 history loaded in {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    main()
