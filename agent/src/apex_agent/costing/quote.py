"""One estimate for an RFQ part (S5): cost for every quantity break, delivery date, CO2 and terms. No LLM.

The live facts come from Odoo through erp-mcp-read as node `cost_leadtime_co2_terms` (OPA decides which tools),
the commercial policy and approved rules from OPA's data, the cycle-time model from MinIO.
"""

import asyncio
import os
from dataclasses import asdict
from datetime import date
from typing import Any

import psycopg

from apex_agent import opa
from apex_agent.costing import co2, cycle_time, leadtime, standards, terms
from apex_agent.costing.engine import Context, Estimate, estimate
from apex_agent.costing.standards import Part
from apex_agent.llm.client import NeedsHuman
from apex_agent.tools import call_tool

NODE = "cost_leadtime_co2_terms"


async def _tool(name: str, args: dict[str, Any]) -> Any:
    # S16: the what-if service calls as its own node (APEX_COSTING_NODE=whatif), with its own read-only tool list in OPA
    ok, res = await call_tool("erp-mcp-read", name, args, os.environ.get("APEX_COSTING_NODE", NODE))
    if not ok:
        raise NeedsHuman(f"erp-mcp-read {name}: {str(res)[:200]}")
    return res


def _price_on(entries: list[dict[str, Any]], on: date) -> dict[str, Any] | None:
    """The supplier price list entry valid on `on`: the latest one that started on or before it."""
    valid = [e for e in entries if e.get("valid_from") and str(e["valid_from"]) <= on.isoformat()] or entries
    return max(valid, key=lambda e: str(e.get("valid_from") or "")) if valid else None


def load_context(opa_url: str, materials: list[str], outside: list[str], customer_ref: str | None, on: date,
                 model: cycle_time.Model | None = None) -> tuple[Context, dict[str, Any]]:  # fmt: skip
    """(Context, open workload) for a quote dated `on`."""
    facts = fetch_facts(opa_url, materials, outside, customer_ref)
    return build_context(facts, on, model or cycle_time.load()), facts["load"]


def fetch_facts(opa_url: str, materials: list[str], outside: list[str], customer_ref: str | None) -> dict[str, Any]:
    """Everything live, once. Fails closed: no policy from OPA -> NeedsHuman (nobody prices without policy)."""
    policy = opa.decide(opa_url, "policy/pricing_policy", {})
    if not isinstance(policy, dict):
        raise NeedsHuman("OPA returned no pricing policy: the estimate waits (fail closed)")
    rules = opa.decide(opa_url, "policy/pricing_rules", {}) or []

    async def gather() -> tuple[Any, ...]:
        wcs = await _tool("list_workcenters", {})
        mats = {
            m: (await _tool("material_info", {"code": m}), await _tool("get_supplier_prices", {"code": m})) for m in materials
        }
        osps = {o: await _tool("get_supplier_prices", {"code": o}) for o in outside}
        cust = await _tool("get_customer", {"ref": customer_ref}) if customer_ref else {}
        load = await _tool("open_workload", {})
        return wcs, mats, osps, cust, load

    wcs, mats, osps, cust, load = asyncio.run(gather())
    return {"policy": policy, "rules": list(rules), "wcs": wcs, "mats": mats, "osps": osps, "cust": cust, "load": load,
            "customer_ref": customer_ref}  # fmt: skip


def build_context(f: dict[str, Any], on: date, model: cycle_time.Model) -> Context:
    """The context for a quote dated `on`: supplier prices valid on that day."""
    mats, osps, cust = f["mats"], f["osps"], f["cust"]
    mat_ctx = {}
    for code, (info, prices) in mats.items():
        if not info.get("found"):
            raise NeedsHuman(f"material {code} is not in Odoo")
        e = _price_on(prices, on)
        mat_ctx[code] = {"density": info["density_g_cm3"], "on_hand": info["on_hand"],
                         "price_per_kg": e["price"] if e else info["standard_price"],
                         "price_source": f"Odoo price list {e['supplier']}, valid from {e['valid_from']}" if e else "Odoo standard cost",
                         "lead_days": int(e["lead_days"]) if e else 14}  # fmt: skip
    osp_ctx = {}
    for code, prices in osps.items():
        e = _price_on(prices, on)
        if not e:
            raise NeedsHuman(f"no supplier price for {code} in Odoo")
        osp_ctx[code] = {"price": e["price"], "lead_days": int(e["lead_days"]), "source": f"Odoo price list {e['supplier']}"}
    fill = {fam: v["fill"] for fam, v in (model.card.get("fill_factor_by_family") or {}).items()}
    return Context({w["code"]: w for w in f["wcs"]}, mat_ctx, osp_ctx, f["policy"], f["rules"], model, fill,
                   {"ref": f["customer_ref"], "name": cust.get("name"), "tier": cust.get("tier")} if cust.get("found") else {})  # fmt: skip


def quote_part(conn: psycopg.Connection[Any], part: Part, quantities: list[int], ctx: Context, workload: dict[str, Any],
               on: date, requested: str | None, docs: dict[str, bool], co2_requested: bool) -> dict[str, Any]:  # fmt: skip
    breaks: list[Estimate] = [estimate(part, q, ctx, docs) for q in quantities]
    main = breaks[0]
    route = list(main.minutes)
    mat = ctx.materials[part.material]
    scrap = standards.shop()["stock"]["scrap_factor"][part.process]
    ready = leadtime.material_ready(mat["on_hand"], main.stock_kg * scrap * main.qty, mat["lead_days"], on)
    outside_days = max((ctx.outside[o]["lead_days"] for o in part.outside), default=0)
    plan = leadtime.plan(route, main.minutes, main.qty, on, workload, ready, outside_days, ctx.policy)
    fp = co2.footprint(part.material, main.stock_kg, scrap, {wc: m[1] for wc, m in main.minutes.items()},
                       {wc: ctx.workcenters[wc].get("x_power_kw") or 0 for wc in route}, part.outside, main.qty)  # fmt: skip
    material_usd = next(ln.amount for ln in main.lines if ln.name == "material")
    t = terms.terms(conn, part.material, material_usd, on, ctx.policy)
    flags = []
    if requested and plan.committed.isoformat() > requested:
        flags.append(f"requested {requested} is not feasible: earliest committed date {plan.committed.isoformat()} "
                     f"(rush {plan.rush.isoformat()}, +{ctx.policy['rush_premium_pct']} %)")  # fmt: skip
    return {
        "part": asdict(part), "breaks": [{"qty": e.qty, "unit_cost": e.unit_cost, "unit_price": e.unit_price, "margin_pct": round(e.margin_pct, 2),
                                          "rules": e.rules_applied, "lines": [asdict(ln) for ln in e.lines]} for e in breaks],
        "minutes_per_part": {wc: [round(x, 2) for x in m] for wc, m in main.minutes.items()},
        "delivery": {"p50": plan.p50.isoformat(), "committed": plan.committed.isoformat(), "rush": plan.rush.isoformat(),
                     "rush_premium_pct": ctx.policy["rush_premium_pct"], "bottleneck": plan.bottleneck,
                     "material_ready": plan.material_ready.isoformat(), "steps": plan.steps, "requested": requested},
        "co2": {**asdict(fp), "include_in_quote": co2_requested},
        "terms": t, "flags": flags, "model_version": ctx.model.card.get("version"),
    }  # fmt: skip


def analog_check(conn: psycopg.Connection[Any], part: Part, unit_price: float, policy: dict[str, Any]) -> dict[str, Any]:
    """Is the price in line with what similar parts (same family and material, S2 hybrid search) were WON at?
    Compared per kg of stock, so a bigger analog doesn't look 'more expensive': each analog's size comes from Apex's own
    drawing of it (part_specs). A gap beyond the policy % is a risk flag for the reviewer."""
    from apex_agent.knowledge.search import search

    text = f"{part.family.replace('_', ' ')} {part.material} {part.length:g} x {part.width:g} x {part.height:g} mm {part.holes} holes"
    rows = search(conn, text, k=5, material=part.material, family=part.family)
    _, mine = standards.stock(part)
    per_kg = []
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("SELECT envelope FROM part_specs WHERE code = %s", (r["code"],))
            env = (cur.fetchone() or [None])[0]
            if not env or not r.get("won_prices"):
                continue
            _, theirs = standards.stock(Part(part.family, part.material, *env))
            per_kg += [float(p) / theirs for p in r["won_prices"]]
    if not per_kg:
        return {"analogs": [r["code"] for r in rows], "won_per_stock_cm3": None, "gap_pct": None, "flag": None}
    med = sorted(per_kg)[len(per_kg) // 2]
    ratio = unit_price / mine / med
    gap = (ratio - 1) * 100
    flag = None
    limit = float(policy["analog_gap_flag_ratio"])
    if ratio > limit or ratio < 1 / limit:
        flag = (f"price ${unit_price:.2f} is {gap:+.0f} % per unit of stock from what analog parts were won at "
                f"({', '.join(r['code'] for r in rows)}): check the inputs")  # fmt: skip
    return {"analogs": [r["code"] for r in rows], "won_per_stock_cm3": round(med, 4), "gap_pct": round(gap, 1), "flag": flag}
