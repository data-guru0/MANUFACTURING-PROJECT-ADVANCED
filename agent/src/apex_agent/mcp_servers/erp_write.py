"""erp-mcp-write: the ONLY write path into Odoo, as `erp_integration` (S2; used by `act` in S8).

Two locks on every tool: OPA's tool policy (only the `act` node) AND, from S8, a person's signed approval of exactly this
content (OPA apex/actions: the token from the review screen, the prices hashed HERE from what we are asked to write).
Everything stays a DRAFT: nothing is confirmed, nothing is sent from Odoo."""

import base64
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from apex_agent.actions.approval import prices_sha256
from apex_agent.mcp_servers.common import guard, require_approval, serve
from apex_agent.odoo import Odoo
from apex_agent.settings import OdooSettings, load_as

mcp: MCPServer[Any] = MCPServer("erp-mcp-write", instructions="Create DRAFT documents in Apex's Odoo, after approval.")


def order_ref(email_id: int) -> str:
    """The idempotency key: one quotation per RFQ email, ever (client_order_ref)."""
    return f"AGENT-RFQ-{email_id}"


def attach(o: Odoo, order_id: int, files: list[dict[str, Any]]) -> int:
    """[{name, mimetype, data_b64}] onto the quotation; a file already attached under that name is skipped."""
    have = {
        a["name"]
        for a in o.call(
            "ir.attachment", "search_read", [("res_model", "=", "sale.order"), ("res_id", "=", order_id)], fields=["name"]
        )
    }
    n = 0
    for f in files:
        if f["name"] in have:
            continue
        base64.b64decode(f["data_b64"], validate=True)  # refuse anything that isn't base64
        o.call("ir.attachment", "create", {"name": f["name"], "datas": f["data_b64"], "mimetype": f.get("mimetype") or "application/octet-stream",
                                           "res_model": "sale.order", "res_id": order_id})  # fmt: skip
        n += 1
    return n


@mcp.tool()
def create_draft_quotation(email_id: int, approval: str, customer: dict[str, Any], lines: list[dict[str, Any]],
                           validity_date: str, note_html: str, ctx: Context[Any, Any], rfq_received_at: str = "",
                           descriptions: dict[str, str] | None = None,
                           attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    """A DRAFT quotation for one RFQ email. `lines`: [{code, breaks: [{qty, unit_price}]}]; the first break is the order
    line, the other breaks are optional lines (Odoo's quotation options). `customer`: {ref} of a known customer, or
    {name, email} of a new one (created as a partner). New part codes are created as products. Idempotent by email."""
    guard(ctx, "erp.create_draft_quotation")
    require_approval("erp.create_draft_quotation", email_id, approval, prices_sha256=prices_sha256(lines))
    o = Odoo(load_as(OdooSettings))
    ref = order_ref(email_id)
    have = o.call("sale.order", "search_read", [("client_order_ref", "=", ref)], fields=["name", "state"], limit=1)
    if have:
        return {"created": False, "id": have[0]["id"], "name": have[0]["name"], "state": have[0]["state"]}
    partner = None
    if customer.get("ref"):
        partner = (o.call("res.partner", "search", [("ref", "=", customer["ref"])], limit=1) or [None])[0]
    if not partner and customer.get("email"):
        partner = (o.call("res.partner", "search", [("email", "=ilike", customer["email"])], limit=1) or [None])[0]
    if not partner:
        if not customer.get("email"):
            raise ToolError("no customer: give a known ref or a name and email")
        partner = o.call("res.partner", "create", {"name": customer.get("name") or customer["email"], "email": customer["email"],
                                                    "customer_rank": 1, "comment": "Created by the quoting agent (new prospect)"})  # fmt: skip
    categ = (o.call("product.category", "search", [("name", "=", "Customer Parts")], limit=1) or [False])[0]
    order_lines, options = [], []
    for ln in lines:
        found = o.call("product.product", "search_read", [("default_code", "=", ln["code"])], fields=["id"], limit=1)
        if found:
            prod = found[0]["id"]
        else:  # a new part: a product like the ones Apex already has
            desc = (descriptions or {}).get(ln["code"]) or ln["code"]
            tmpl = o.call("product.template", "create", {"name": desc, "default_code": ln["code"], "detailed_type": "product",
                                                          "sale_ok": True, "purchase_ok": False, "categ_id": categ, "x_seed_batch": "agent"})  # fmt: skip
            prod = o.call("product.template", "read", [tmpl], ["product_variant_id"])[0]["product_variant_id"][0]
        name = f"[{ln['code']}] {(descriptions or {}).get(ln['code']) or ''}".strip()
        first, *more = ln["breaks"]
        order_lines.append(
            (0, 0, {"product_id": prod, "name": name, "product_uom_qty": first["qty"], "price_unit": first["unit_price"]})
        )
        for b in more:
            options.append((0, 0, {"product_id": prod, "name": f"{name} — price for {b['qty']} pcs", "quantity": b["qty"],
                                   "price_unit": b["unit_price"]}))  # fmt: skip
        for opt in ln.get("options") or []:  # S13: DFM alternatives, optional; the order line keeps the customer's spec
            options.append(
                (0, 0, {"product_id": prod, "name": opt["label"], "quantity": opt["qty"], "price_unit": opt["unit_price"]})
            )
    vals = {"partner_id": partner, "client_order_ref": ref, "order_line": order_lines, "sale_order_option_ids": options,
            "validity_date": validity_date, "note": note_html, "x_seed_batch": "agent"}  # fmt: skip
    if rfq_received_at:
        vals["x_rfq_received_at"] = rfq_received_at
    so = o.call("sale.order", "create", vals)
    attached = attach(o, so, attachments or [])
    return {
        "created": True,
        "id": so,
        "name": o.call("sale.order", "read", [so], ["name"])[0]["name"],
        "state": "draft",
        "attached": attached,
    }


@mcp.tool()
def mark_quote_sent(email_id: int, approval: str, sent_at: str, ctx: Context[Any, Any],
                    attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    """After the reply went out: x_quote_sent_at on THIS email's quotation (found by its key, never by a name the caller
    picks), plus the quote PDF as an attachment. Stays a draft quotation."""
    guard(ctx, "erp.mark_quote_sent")
    require_approval("erp.mark_quote_sent", email_id, approval)
    o = Odoo(load_as(OdooSettings))
    found = o.call("sale.order", "search_read", [("client_order_ref", "=", order_ref(email_id))], fields=["name"], limit=1)
    if not found:
        raise ToolError(f"no quotation for email {email_id}")
    o.call("sale.order", "write", [found[0]["id"]], {"x_quote_sent_at": sent_at})
    return {"name": found[0]["name"], "x_quote_sent_at": sent_at, "attached": attach(o, found[0]["id"], attachments or [])}


@mcp.tool()
def record_lost(name: str, reason: str, email_id: int, ctx: Context[Any, Any]) -> dict[str, Any]:
    """S15: the customer declined: the quotation is cancelled with their lost reason (W9). Internal bookkeeping, nothing
    leaves Apex, so no approval token; OPA's tool policy decides who may call it. Never touches a confirmed order."""
    guard(ctx, "erp.record_lost")
    o = Odoo(load_as(OdooSettings))
    found = o.call("sale.order", "search_read", [("name", "=", name)], fields=["state", "x_lost_reason"], limit=1)
    if not found:
        raise ToolError(f"no quotation {name}")
    q = found[0]
    if q["state"] not in ("draft", "sent", "cancel"):
        raise ToolError(f"{name} is {q['state']}: only an open quotation can be marked lost")
    reason = f"{reason.strip()[:200]} (customer email #{email_id})"
    # ponytail: a plain state write (Odoo's cancel wizard is interactive); quotations have no stock or invoices to undo
    o.call("sale.order", "write", [q["id"]], {"state": "cancel", "x_lost_reason": reason})
    return {"name": name, "state": "cancel", "x_lost_reason": reason, "was": q["state"]}


@mcp.tool()
def mark_expired(ctx: Context[Any, Any]) -> dict[str, Any]:
    """S15: open quotations (draft/sent) past their validity date get the tag "Expired" (Odoo keeps them; a person
    decides whether to re-quote). Idempotent."""
    guard(ctx, "erp.mark_expired")
    o = Odoo(load_as(OdooSettings))
    tag = (o.call("crm.tag", "search", [("name", "=", "Expired")], limit=1) or [None])[0] or o.call(
        "crm.tag", "create", {"name": "Expired"}
    )
    today = datetime.now(UTC).date().isoformat()
    ids = o.call("sale.order", "search", [("state", "in", ["draft", "sent"]), ("validity_date", "<", today),
                                          ("tag_ids", "not in", [tag])])  # fmt: skip
    if ids:
        o.call("sale.order", "write", ids, {"tag_ids": [(4, tag)]})
    names = [r["name"] for r in o.call("sale.order", "read", ids, ["name"])] if ids else []
    return {"tagged": len(names), "quotations": names, "tag": "Expired", "as_of": today}


@mcp.tool()
def record_supplier_quote(code: str, vendor_ref: str, price: float, lead_days: int, ctx: Context[Any, Any]) -> dict[str, Any]:
    """S17 (W13): a supplier's quoted price becomes a NEW price-list entry valid from today, so the next estimate uses
    it (the old entries stay: history). Internal bookkeeping, no approval token; OPA decides who may call it."""
    guard(ctx, "erp.record_supplier_quote")
    o = Odoo(load_as(OdooSettings))
    vendor = o.call("res.partner", "search", [("ref", "=", vendor_ref)], limit=1)
    tmpl = o.call("product.template", "search", [("default_code", "=", code)], limit=1)
    if not vendor or not tmpl or price <= 0:
        raise ToolError(f"unknown vendor {vendor_ref} or product {code}, or no price")
    today = datetime.now(UTC).date().isoformat()
    sid = o.call("product.supplierinfo", "create", {"partner_id": vendor[0], "product_tmpl_id": tmpl[0], "price": price,
                                                    "delay": int(lead_days), "date_start": today})  # fmt: skip
    return {"supplierinfo": sid, "code": code, "vendor": vendor_ref, "price": price, "lead_days": lead_days, "valid_from": today}


@mcp.tool()
def create_draft_po(vendor_ref: str, code: str, qty: float, price: float, origin: str, ctx: Context[Any, Any]) -> dict[str, Any]:
    """S17: a DRAFT purchase order for the chosen supplier (a person confirms it in Odoo; nothing is sent).
    Idempotent by origin."""
    guard(ctx, "erp.create_draft_po")
    o = Odoo(load_as(OdooSettings))
    have = o.call("purchase.order", "search_read", [("origin", "=", origin)], fields=["name", "state"], limit=1)
    if have:
        return {"created": False, "name": have[0]["name"], "state": have[0]["state"]}
    vendor = o.call("res.partner", "search", [("ref", "=", vendor_ref)], limit=1)
    prod = o.call("product.product", "search", [("default_code", "=", code)], limit=1)
    if not vendor or not prod:
        raise ToolError(f"unknown vendor {vendor_ref} or product {code}")
    po = o.call("purchase.order", "create", {"partner_id": vendor[0], "origin": origin, "order_line": [
        (0, 0, {"product_id": prod[0], "product_qty": qty, "price_unit": price})]})  # fmt: skip
    return {"created": True, "name": o.call("purchase.order", "read", [po], ["name"])[0]["name"], "state": "draft"}


def main() -> None:
    serve(mcp, "erp-mcp-write")


if __name__ == "__main__":
    main()
