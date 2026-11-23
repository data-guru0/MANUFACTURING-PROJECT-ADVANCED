"""L2 Done-when, against the live Odoo: counts, idempotency, order totals and every seeded weakness.

Everything is read back from Odoo itself (not from the generator), the way an FDE would audit it.
"""

import difflib
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Any

import pytest

from legacy.odoo.rpc import Odoo, env

FMT = "%Y-%m-%d %H:%M:%S"
SEED = "L2-history"


def dt(s: str) -> datetime:
    return datetime.strptime(s, FMT)


@pytest.fixture(scope="module")
def o() -> Odoo:
    return Odoo("admin", env("ODOO_ADMIN_PASSWORD"))


@pytest.fixture(scope="module")
def sos(o: Odoo) -> list[dict[str, Any]]:
    return list(o.call("sale.order", "search_read", [("client_order_ref", "=like", "HIST-%")], fields=[
        "name", "state", "amount_untaxed", "date_order", "commitment_date", "effective_date",
        "x_quote_sent_at", "x_lost_reason", "x_seed_batch", "order_line"]))  # fmt: skip


def counts(o: Odoo) -> dict[str, int]:
    return {
        "partners": o.call("res.partner", "search_count", [("x_seed_batch", "=", SEED)]),
        "products": o.call("product.template", "search_count", [("x_seed_batch", "=", SEED)]),
        "boms": o.call("mrp.bom", "search_count", [("product_tmpl_id.x_seed_batch", "=", SEED)]),
        "sale_orders": o.call("sale.order", "search_count", [("client_order_ref", "=like", "HIST-%")]),
        "purchase_orders": o.call("purchase.order", "search_count", [("x_seed_batch", "=", SEED)]),
        "mos": o.call("mrp.production", "search_count", [("x_seed_batch", "=", SEED)]),
        "supplierinfo": o.call("product.supplierinfo", "search_count", [("partner_id.x_seed_batch", "=", SEED)]),
    }  # fmt: skip


def test_counts(o: Odoo, sos: list[dict[str, Any]]) -> None:
    customers = o.call("res.partner", "search_read", [("x_seed_batch", "=", SEED), ("customer_rank", ">", 0)],
                       fields=["ref"])  # fmt: skip
    real = [c for c in customers if not c["ref"].endswith("-DUP")]
    assert 30 <= len(real) <= 40
    assert o.call("res.partner", "search_count", [("x_seed_batch", "=", SEED), ("supplier_rank", ">", 0)]) == 12
    assert len(sos) == 400
    assert all(s["x_seed_batch"] == SEED for s in sos), "a quote was left half-loaded"
    assert o.call("product.template", "search_count", [("x_seed_batch", "=", SEED)]) >= 300


def test_win_rate_about_25_to_30_percent(sos: list[dict[str, Any]]) -> None:
    won = sum(1 for s in sos if s["state"] == "sale")
    lost = sum(1 for s in sos if s["state"] == "cancel")
    assert 0.25 <= won / (won + lost) <= 0.30


def test_won_order_totals_equal_line_sums(o: Odoo, sos: list[dict[str, Any]]) -> None:
    won = [s for s in sos if s["state"] == "sale"]
    lines = o.call("sale.order.line", "search_read", [("order_id", "in", [s["id"] for s in won])],
                   fields=["order_id", "price_subtotal", "price_unit", "product_uom_qty"])  # fmt: skip
    total: dict[int, float] = {}
    for ln in lines:
        assert ln["price_subtotal"] == pytest.approx(ln["price_unit"] * ln["product_uom_qty"], abs=0.01)
        total[ln["order_id"][0]] = total.get(ln["order_id"][0], 0.0) + ln["price_subtotal"]
    for s in won:
        assert s["amount_untaxed"] == pytest.approx(total[s["id"]], abs=0.01), s["name"]


def test_w4_duplicates_name_variants_missing_routings(o: Odoo) -> None:
    dups = o.call("res.partner", "search_read", [("ref", "=like", "CUST-%-DUP")], fields=["name", "ref"])
    assert len(dups) == 5
    for d in dups:
        orig = o.call("res.partner", "search_read", [("ref", "=", d["ref"][:-4])], fields=["name"])[0]
        similar = difflib.SequenceMatcher(None, d["name"].lower(), orig["name"].lower()).ratio()
        assert d["name"] != orig["name"] and similar > 0.6, (d["name"], orig["name"])
    grades = {p["x_material_grade"] for p in o.call("product.template", "search_read", [
        ("x_seed_batch", "=", SEED), ("bom_ids.bom_line_ids.product_id.default_code", "=", "MAT-AL6061")],
        fields=["x_material_grade"])}  # fmt: skip
    assert len(grades) >= 4, f"expected messy names for Al 6061, got {grades}"
    boms = o.call("mrp.bom", "search_read", [("product_tmpl_id.x_seed_batch", "=", SEED)],
                  fields=["operation_ids"])  # fmt: skip
    share = sum(1 for b in boms if not b["operation_ids"]) / len(boms)
    assert 0.12 <= share <= 0.18


def test_w7_fixed_three_week_promise_and_about_70pct_on_time(sos: list[dict[str, Any]]) -> None:
    won = [s for s in sos if s["state"] == "sale"]
    assert all(dt(s["commitment_date"]) - dt(s["date_order"]) == timedelta(days=21) for s in won)
    delivered = [s for s in won if s["effective_date"]]
    on_time = sum(1 for s in delivered if dt(s["effective_date"]) <= dt(s["commitment_date"]))
    assert len(delivered) >= 80
    assert 0.62 <= on_time / len(delivered) <= 0.78


def test_w8_late_acceptances_at_old_prices_lost_money(o: Odoo, sos: list[dict[str, Any]]) -> None:
    rate = {w["id"]: w["costs_hour"] for w in o.call("mrp.workcenter", "search_read", [], fields=["costs_hour"])}
    late = [s for s in sos if s["state"] == "sale" and dt(s["date_order"]) - dt(s["x_quote_sent_at"]) > timedelta(days=90)]
    losses = 0
    for s in late:
        material = sum(p["amount_untaxed"] for p in o.call("purchase.order", "search_read",
                       [("origin", "=", s["name"]), ("partner_ref", "=like", "%-MAT")], fields=["amount_untaxed"]))  # fmt: skip
        wos = o.call("mrp.workorder", "search_read", [("production_id.origin", "=", s["name"])],
                     fields=["duration", "workcenter_id"])  # fmt: skip
        machine = sum(w["duration"] / 60 * rate[w["workcenter_id"][0]] for w in wos)
        # material + machine time + 15 % overhead alone already exceed the sale (outside processes ignored)
        if (material + machine) * 1.15 > s["amount_untaxed"]:
            losses += 1
    assert 8 <= len(late) <= 12 and losses >= 8, (len(late), losses)


def test_w9_lost_reason_on_about_20pct(sos: list[dict[str, Any]]) -> None:
    lost = [s for s in sos if s["state"] == "cancel"]
    share = sum(1 for s in lost if s["x_lost_reason"]) / len(lost)
    assert 0.15 <= share <= 0.25


def test_w11_outsourced_at_a_loss(o: Odoo, sos: list[dict[str, Any]]) -> None:
    by_name = {s["name"]: s for s in sos}
    subcon = o.call("purchase.order", "search_read", [("partner_id.ref", "=", "SUP-12")],
                    fields=["origin", "amount_untaxed", "state"])  # fmt: skip
    assert len(subcon) >= 3
    for po in subcon:
        assert po["amount_untaxed"] > by_name[po["origin"]]["amount_untaxed"]


def test_w13_supplier_prices_often_stale(o: Odoo) -> None:
    # Price-list lines Tom entered (dated). Odoo also auto-adds an undated line when a PO is
    # confirmed for a new vendor/product (the subcontractor); those are not price lists.
    rows = o.call("product.supplierinfo", "search_read", [("partner_id.x_seed_batch", "=", SEED),
                  ("date_start", "!=", False)], fields=["date_start"])  # fmt: skip
    cutoff = datetime.now() - timedelta(days=182)
    stale = sum(1 for r in rows if datetime.strptime(r["date_start"], "%Y-%m-%d") < cutoff)
    assert len(rows) >= 20 and stale / len(rows) >= 0.3


def test_w14_actual_times_differ_from_estimate(o: Odoo) -> None:
    wos = o.call("mrp.workorder", "search_read", [("production_id.state", "=", "done")],
                 fields=["duration", "duration_expected"])  # fmt: skip
    assert len(wos) >= 200
    dev = [abs(w["duration"] / w["duration_expected"] - 1) for w in wos if w["duration_expected"]]
    assert sum(dev) / len(dev) > 0.05
    scrap = o.call("stock.move", "search_read", [("raw_material_production_id.state", "=", "done")],
                   fields=["quantity", "product_uom_qty"])  # fmt: skip
    assert sum(1 for m in scrap if m["quantity"] > m["product_uom_qty"] * 1.001) / len(scrap) > 0.5


def test_open_work_loads_main_machines_60_to_90pct_for_6_weeks(o: Odoo) -> None:
    now, horizon = datetime.now(), datetime.now() + timedelta(days=42)
    wos = o.call("mrp.workorder", "search_read", [
        ("production_id.state", "in", ["confirmed", "progress"]),
        ("production_id.date_start", ">=", (now - timedelta(days=1)).strftime(FMT)),
        ("production_id.date_start", "<", horizon.strftime(FMT))],
        fields=["workcenter_id", "duration_expected"])  # fmt: skip
    codes = {w["id"]: w["code"] for w in o.call("mrp.workcenter", "search_read", [], fields=["code"])}
    load: dict[str, float] = {}
    for w in wos:
        code = codes[w["workcenter_id"][0]]
        load[code] = load.get(code, 0.0) + w["duration_expected"]
    capacity = 6 * 5 * 16 * 60
    for wc in ["MILL3", "MILL5", "LATHE", "LASER"]:
        assert 0.60 <= load.get(wc, 0) / capacity <= 0.90, (wc, load.get(wc, 0) / capacity)


def test_revisions_and_customer_flags(o: Odoo) -> None:
    codes = [p["default_code"] for p in o.call("product.template", "search_read", [("x_seed_batch", "=", SEED)],
             fields=["default_code"])]  # fmt: skip
    with_b = {c[:-2] for c in codes if c.endswith("-B")}
    assert len({c[:-2] for c in codes if c.endswith("-A")} & with_b) >= 40
    cust = o.call("res.partner", "search_read", [("x_seed_batch", "=", SEED), ("customer_rank", ">", 0)],
                  fields=["x_nda_restricted", "x_required_certs", "x_language"])  # fmt: skip
    assert any(c["x_nda_restricted"] for c in cust)
    assert any(c["x_required_certs"] for c in cust)
    assert len({c["x_language"] for c in cust}) >= 3


def test_seed_is_idempotent(o: Odoo) -> None:
    before = counts(o)
    subprocess.run([sys.executable, "-m", "legacy.seed.load_odoo"], check=True, capture_output=True)
    assert counts(o) == before
