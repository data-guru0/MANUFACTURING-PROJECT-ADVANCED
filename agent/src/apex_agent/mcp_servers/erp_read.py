"""erp-mcp-read: read-only tools over Odoo as `erp_reader` (S2). Odoo stays the system of record."""

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from apex_agent.mcp_servers.common import guard, serve
from apex_agent.odoo import Odoo
from apex_agent.settings import OdooSettings, load_as

mcp: MCPServer[Any] = MCPServer("erp-mcp-read", instructions="Read Apex's Odoo: products, BOMs, routings, rates, suppliers.")
_odoo: list[Odoo] = []


def odoo() -> Odoo:
    if not _odoo:
        _odoo.append(Odoo(load_as(OdooSettings)))
    return _odoo[0]


@mcp.tool()
def search_products(text: str, ctx: Context[Any, Any], limit: int = 10) -> list[dict[str, Any]]:
    """Find products whose code or name contains `text`."""
    guard(ctx, "erp.search_products")
    domain = ["|", ("default_code", "ilike", text), ("name", "ilike", text)]
    fields = ["default_code", "name", "x_part_family", "x_material_grade", "x_drawing_no"]
    rows: list[dict[str, Any]] = odoo().call("product.template", "search_read", domain, fields=fields, limit=min(limit, 50))
    return rows


@mcp.tool()
def get_product(code: str, ctx: Context[Any, Any]) -> dict[str, Any]:
    """One product with its BOM materials and routing operations, by internal reference."""
    guard(ctx, "erp.get_product")
    o = odoo()
    fields = ["default_code", "name", "x_part_family", "x_material_grade", "x_drawing_no", "bom_ids"]
    found = o.call("product.template", "search_read", [("default_code", "=", code)], fields=fields, limit=1)
    if not found:
        return {"found": False, "code": code}
    p = found[0]
    bom = o.call("mrp.bom", "read", p["bom_ids"][:1], ["bom_line_ids", "operation_ids"]) if p["bom_ids"] else []
    lines = o.call("mrp.bom.line", "read", bom[0]["bom_line_ids"], ["product_id", "product_qty"]) if bom else []
    ops = (
        o.call("mrp.routing.workcenter", "read", bom[0]["operation_ids"], ["name", "workcenter_id", "time_cycle_manual"])
        if bom
        else []
    )
    return {
        "found": True, "code": code, "name": p["name"], "family": p["x_part_family"], "material_text": p["x_material_grade"],
        "drawing": p["x_drawing_no"],
        "materials": [{"product": ln["product_id"][1], "qty": ln["product_qty"]} for ln in lines],
        "routing": [{"operation": op["name"], "workcenter": op["workcenter_id"][1], "minutes": op["time_cycle_manual"]} for op in ops],
    }  # fmt: skip


@mcp.tool()
def get_drawing(code: str, ctx: Context[Any, Any]) -> dict[str, Any]:
    """S12: Apex's archived drawing files of one part (DXF / PDF / STEP attachments), base64, to diff a revision."""
    guard(ctx, "erp.get_drawing")
    o = odoo()
    found = o.call("product.template", "search_read", [("default_code", "=", code)], fields=["id"], limit=1)
    if not found:
        return {"found": False, "code": code}
    atts = o.call("ir.attachment", "search_read", [("res_model", "=", "product.template"), ("res_id", "=", found[0]["id"])],
                  fields=["name", "datas"])  # fmt: skip
    return {"found": True, "code": code, "files": [{"name": a["name"], "data_b64": a["datas"]} for a in atts if a.get("datas")]}


@mcp.tool()
def list_workcenters(ctx: Context[Any, Any]) -> list[dict[str, Any]]:
    """Work centers with hourly rate, setup time, envelope, best tolerance and power."""
    guard(ctx, "erp.list_workcenters")
    fields = ["code", "name", "costs_hour", "time_start", "time_stop", "x_max_x_mm", "x_max_y_mm", "x_max_z_mm", "x_max_weight_kg",
              "x_best_tolerance_mm", "x_best_ra", "x_power_kw"]  # fmt: skip
    rows: list[dict[str, Any]] = odoo().call("mrp.workcenter", "search_read", [], fields=fields)
    return rows


@mcp.tool()
def get_supplier_prices(code: str, ctx: Context[Any, Any]) -> list[dict[str, Any]]:
    """Supplier price-list entries for a product code, newest first, with their start date (age matters: W13)."""
    guard(ctx, "erp.get_supplier_prices")
    rows = odoo().call("product.supplierinfo", "search_read", [("product_tmpl_id.default_code", "=", code)],
                       fields=["partner_id", "price", "delay", "date_start"], order="id desc")  # fmt: skip
    return [
        {"supplier": r["partner_id"][1], "price": r["price"], "lead_days": r["delay"], "valid_from": r["date_start"]}
        for r in rows
    ]


@mcp.tool()
def find_customer(name: str, ctx: Context[Any, Any]) -> list[dict[str, Any]]:
    """Customers whose name contains `name`, with NDA, certificate and language flags."""
    guard(ctx, "erp.find_customer")
    fields = ["ref", "name", "email", "x_nda_restricted", "x_required_certs", "x_language"]
    rows: list[dict[str, Any]] = odoo().call("res.partner", "search_read", [("name", "ilike", name), ("customer_rank", ">", 0)],
                                             fields=fields, limit=10)  # fmt: skip
    return rows


@mcp.tool()
def customer_by_email(email: str, ctx: Context[Any, Any]) -> list[dict[str, Any]]:
    """Customers whose email address is exactly `email` (case-insensitive): is this sender a known customer?"""
    guard(ctx, "erp.customer_by_email")
    fields = ["ref", "name", "email", "x_nda_restricted", "x_required_certs", "x_language"]
    rows: list[dict[str, Any]] = odoo().call("res.partner", "search_read", [("email", "=ilike", email.strip()), ("customer_rank", ">", 0)],
                                             fields=fields, limit=5)  # fmt: skip
    return rows


@mcp.tool()
def material_info(code: str, ctx: Context[Any, Any]) -> dict[str, Any]:
    """A raw material: density, standard cost, unit of measure and the quantity on hand (S5)."""
    guard(ctx, "erp.material_info")
    o = odoo()
    found = o.call("product.template", "search_read", [("default_code", "=", code)],
                   fields=["name", "x_density_g_cm3", "standard_price", "uom_id", "product_variant_id"], limit=1)  # fmt: skip
    if not found:
        return {"found": False, "code": code}
    p = found[0]
    quants = o.call("stock.quant", "search_read", [("product_id", "=", p["product_variant_id"][0]), ("location_id.usage", "=", "internal")],
                    fields=["quantity"])  # fmt: skip
    return {"found": True, "code": code, "name": p["name"], "density_g_cm3": p["x_density_g_cm3"],
            "standard_price": p["standard_price"], "uom": p["uom_id"][1], "on_hand": sum(q["quantity"] for q in quants)}  # fmt: skip


@mcp.tool()
def get_customer(ref: str, ctx: Context[Any, Any]) -> dict[str, Any]:
    """A customer's commercial facts by internal reference: tier (partner tag 'Tier X'), certificates, language."""
    guard(ctx, "erp.get_customer")
    o = odoo()
    rows = o.call("res.partner", "search_read", [("ref", "=", ref)],
                  fields=["ref", "name", "category_id", "x_required_certs", "x_language", "x_nda_restricted"], limit=1)  # fmt: skip
    if not rows:
        return {"found": False, "ref": ref}
    c = rows[0]
    tags = [t["name"] for t in o.call("res.partner.category", "read", c["category_id"], ["name"])] if c["category_id"] else []
    tier = next((t.removeprefix("Tier ").strip() for t in tags if t.startswith("Tier ")), None)
    return {"found": True, "ref": c["ref"], "name": c["name"], "tier": tier, "certs": c["x_required_certs"] or "",
            "language": c["x_language"], "nda_restricted": c["x_nda_restricted"]}  # fmt: skip


@mcp.tool()
def quote_status(refs: list[str], ctx: Context[Any, Any]) -> list[dict[str, Any]]:
    """S8 learn: the state of the agent's quotations (client_order_ref): draft/sent, sale (won) or cancel (lost)."""
    guard(ctx, "erp.quote_status")
    rows: list[dict[str, Any]] = odoo().call("sale.order", "search_read", [("client_order_ref", "in", refs)],
                                             fields=["name", "client_order_ref", "state", "x_lost_reason", "amount_untaxed", "date_order"])  # fmt: skip
    return rows


@mcp.tool()
def list_vendors(code: str, ctx: Context[Any, Any], limit: int = 3) -> list[dict[str, Any]]:
    """S17: who to ask for a product: vendors whose Odoo price list has it (newest first), then vendors of the same kind
    (material / outside process), up to `limit`."""
    guard(ctx, "erp.list_vendors")
    o = odoo()
    rows = o.call(
        "product.supplierinfo", "search_read", [], fields=["partner_id", "product_tmpl_id", "date_start"], order="date_start desc"
    )
    tmpl = {r["product_tmpl_id"][0] for r in rows}
    codes = {p["id"]: p["default_code"] or "" for p in o.call("product.template", "read", list(tmpl), ["default_code"])}
    by_vendor: dict[int, set[str]] = {}
    for r in rows:
        by_vendor.setdefault(r["partner_id"][0], set()).add(codes[r["product_tmpl_id"][0]])
    has = [v for v in dict.fromkeys(r["partner_id"][0] for r in rows if codes[r["product_tmpl_id"][0]] == code)]
    kind = code.split("-")[0] + "-"
    same = [v for v, cs in by_vendor.items() if v not in has and any(c.startswith(kind) for c in cs)]
    pick = (has + same)[:limit]
    partners = {p["id"]: p for p in o.call("res.partner", "read", pick, ["ref", "name", "email"])}
    return [{"ref": partners[v]["ref"], "name": partners[v]["name"], "email": partners[v]["email"], "lists_product": v in has}
            for v in pick]  # fmt: skip


@mcp.tool()
def get_quotation(name: str, ctx: Context[Any, Any]) -> dict[str, Any]:
    """S15: one quotation by its Odoo number (S0xxxx): state, validity, customer, lines (part code, qty, unit price)."""
    guard(ctx, "erp.get_quotation")
    o = odoo()
    found = o.call("sale.order", "search_read", [("name", "=", name)],
                   fields=["name", "state", "validity_date", "partner_id", "client_order_ref", "x_quote_sent_at", "x_lost_reason",
                           "order_line", "tag_ids"], limit=1)  # fmt: skip
    if not found:
        return {"found": False, "name": name}
    q = found[0]
    lines = o.call("sale.order.line", "read", q["order_line"], ["product_id", "product_uom_qty", "price_unit", "display_type"])
    partner = o.call("res.partner", "read", [q["partner_id"][0]], ["ref", "name"])[0] if q["partner_id"] else {}
    return {"found": True, "name": q["name"], "state": q["state"], "validity_date": q["validity_date"] or None,
            "customer_ref": partner.get("ref") or None, "customer_name": partner.get("name"), "client_order_ref": q["client_order_ref"],
            "x_quote_sent_at": q["x_quote_sent_at"] or None, "x_lost_reason": q["x_lost_reason"] or None,
            "lines": [{"code": ln["product_id"][1].split("]")[0].lstrip("[") if ln["product_id"] else "", "qty": ln["product_uom_qty"],
                       "price_unit": ln["price_unit"]} for ln in lines if not ln["display_type"] and ln["product_id"]]}  # fmt: skip


@mcp.tool()
def open_workload(ctx: Context[Any, Any]) -> dict[str, Any]:
    """Planned minutes still to run on each work center (open work orders) and each work center's shift calendar."""
    guard(ctx, "erp.open_workload")
    o = odoo()
    wos = o.call("mrp.workorder", "search_read", [("state", "in", ["pending", "waiting", "ready", "progress"])],
                 fields=["workcenter_id", "duration_expected", "duration"])  # fmt: skip
    centers = o.call("mrp.workcenter", "search_read", [], fields=["code", "resource_calendar_id"])
    code = {c["id"]: c["code"] for c in centers}
    load: dict[str, float] = {}
    for w in wos:
        wc = code[w["workcenter_id"][0]]
        load[wc] = load.get(wc, 0.0) + max(float(w["duration_expected"]) - float(w["duration"] or 0), 0.0)
    calendars: dict[str, dict[str, float]] = {}
    for c in centers:
        att = o.call("resource.calendar.attendance", "search_read", [("calendar_id", "=", c["resource_calendar_id"][0])],
                     fields=["dayofweek", "hour_from", "hour_to"])  # fmt: skip
        hours: dict[str, float] = {}
        for a in att:
            hours[a["dayofweek"]] = hours.get(a["dayofweek"], 0.0) + float(a["hour_to"]) - float(a["hour_from"])
        calendars[c["code"]] = hours  # weekday "0" = Monday -> hours
    return {
        "open_minutes": {k: round(v, 1) for k, v in load.items()},
        "open_work_orders": len(wos),
        "hours_by_weekday": calendars,
    }


def main() -> None:
    serve(mcp, "erp-mcp-read")


if __name__ == "__main__":
    main()
