"""The costing engine (S5): deterministic, and every line says where its number comes from. No LLM here.

  material   stock size (shop standard) x density (Odoo) x $/kg (Odoo supplier price list, else standard cost) x scrap
  setup      (setup + cleanup minutes, Odoo work center) x rate, once per lot (x setups on the main machine) / qty
  run        standard minutes x the cycle-time model's P50 ratio x rate, per operation
  inspection CMM minutes (when the tolerance needs it) x rate / qty
  documents  3.1 certificate, FAI report, PPAP, per lot (policy) / qty
  outside    max(finisher's price x qty, lot minimum) / qty
  overhead   policy % on everything except outside processes
  margin     policy by customer tier (minus steps at quantity), never below the minimum; then approved rules (R-00x)
Then the price for every quantity break, and a risk flag when the price is far from what analog parts sold for.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from apex_agent.costing import standards
from apex_agent.costing.cycle_time import Model
from apex_agent.costing.standards import MAIN, Part


@dataclass
class Line:
    name: str
    amount: float  # $ per part
    provenance: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Context:
    """Live facts from Odoo and the policy from OPA, each with where it came from."""

    workcenters: dict[str, dict[str, Any]]  # code -> costs_hour, time_start, time_stop, x_power_kw
    materials: dict[str, dict[str, Any]]  # code -> price_per_kg, price_source, density, lead_days, on_hand
    outside: dict[str, dict[str, Any]]  # OSP code -> price, source, lead_days
    policy: dict[str, Any]  # pricing_policy (OPA data)
    rules: list[dict[str, Any]]  # approved pricing_rules (R-00x)
    model: Model
    fill: dict[str, float]  # finished / envelope volume by family (calibrated from Apex's STEP models)
    customer: dict[str, Any] = field(default_factory=dict)  # ref, name, tier


@dataclass
class Estimate:
    qty: int
    unit_cost: float
    unit_price: float
    margin_pct: float
    lines: list[Line]
    minutes: dict[str, tuple[float, float, float]]  # per part, P10/P50/P90 by work center (for leadtime and CO2)
    stock_mm: tuple[float, float, float]
    stock_kg: float
    rules_applied: list[str]


def margin_pct(policy: dict[str, Any], tier: str, qty: int) -> float:
    tiers = policy["margin_pct_by_tier"]
    m = float(tiers.get(tier, tiers[policy["default_tier"]]))
    step = max((float(points) for threshold, points in policy["margin_step_down"] if qty >= threshold), default=0.0)
    return max(m - step, float(policy["min_margin_pct"]))


def _rule_applies(rule: dict[str, Any], part: Part, customer: dict[str, Any]) -> bool:
    w = rule.get("when") or {}
    if w.get("materials") and part.material not in w["materials"]:
        return False
    if w.get("customer") and w["customer"].lower() not in str(customer.get("name", "")).lower():
        return False
    return bool(w.get("materials") or w.get("customer"))


def estimate(part: Part, qty: int, ctx: Context, docs: dict[str, bool] | None = None) -> Estimate:
    if qty < 1:
        raise ValueError("qty must be >= 1")
    shop, pol, docs = standards.shop(), ctx.policy, docs or {}
    lines: list[Line] = []
    # material
    size, stock_cm3 = standards.stock(part)
    mat = ctx.materials[part.material]
    kg = stock_cm3 * mat["density"] / 1000.0
    scrap = shop["stock"]["scrap_factor"][part.process]
    lines.append(Line("material", kg * mat["price_per_kg"] * scrap,
                      f"stock {size[0]:g}x{size[1]:g}x{size[2]:g} mm (shop standard sizes) x {mat['density']} g/cm3 (Odoo) x "
                      f"${mat['price_per_kg']}/kg ({mat['price_source']}) x scrap {scrap}",
                      {"stock_mm": size, "kg": round(kg, 4)}))  # fmt: skip
    # run, setup (per work center), with the model's correction
    planned = standards.planned_minutes(part, ctx.fill.get(part.family, 0.5))  # STEP volume wins when there is one
    route = list(planned)
    main = next(wc for wc in route if wc in MAIN)
    setups = shop["families"][part.family]["setups"]
    rows, lot_planned = [], {}
    for wc in route:
        w = ctx.workcenters[wc]
        lot_planned[wc] = float(w["time_start"]) + float(w.get("time_stop") or 0) + qty * planned[wc]
        rows.append(
            {"wc": wc, "family": part.family, "material": part.material, "qty": float(qty), "planned": max(lot_planned[wc], 0.01)}
        )
    ratios = ctx.model.ratios(rows)
    minutes: dict[str, tuple[float, float, float]] = {}
    for wc, (r10, r50, r90) in zip(route, ratios, strict=True):
        w = ctx.workcenters[wc]
        rate = float(w["costs_hour"])
        setup_min = (float(w["time_start"]) + float(w.get("time_stop") or 0)) * (setups if wc == main else 1)
        run_min = qty * planned[wc]
        minutes[wc] = tuple(r * (setup_min + run_min) / qty for r in (r10, r50, r90))  # type: ignore[assignment]
        if setup_min:
            lines.append(Line(f"setup {wc}", r50 * setup_min / 60.0 * rate / qty,
                              f"{setup_min:g} min setup+cleanup (Odoo {wc}) x model ratio {r50:.2f} x ${rate:g}/h (Odoo) / {qty}"))  # fmt: skip
        if run_min:
            lines.append(Line(f"run {wc}", r50 * planned[wc] / 60.0 * rate,
                              f"{planned[wc]:.2f} min standard (costing.yaml) x model ratio {r50:.2f} "
                              f"(cycle-time model {ctx.model.card.get('version', '?')}, P10-P90 {r10:.2f}-{r90:.2f}) x ${rate:g}/h (Odoo {wc})"))  # fmt: skip
    insp = standards.inspection_minutes_per_lot(part, qty)
    if insp and "CMM" in ctx.workcenters:
        rate = float(ctx.workcenters["CMM"]["costs_hour"])
        lines.append(Line("inspection", insp / 60.0 * rate / qty,
                          f"CMM {insp:.1f} min per lot (features x 0.5 min x (1 + 10 % of the lot), tolerance <= 0.02) x ${rate:g}/h"))  # fmt: skip
    for key, wanted in docs.items():
        if wanted:
            usd = float(pol["certificate_usd_per_lot"][key])
            lines.append(Line(f"document {key}", usd / qty, f"${usd:g} per lot (policy certificate_usd_per_lot.{key}) / {qty}"))
    base = sum(ln.amount for ln in lines)
    for osp in part.outside:
        o = ctx.outside[osp]
        lot_min = float(shop["outside_process_lot_min_usd"][osp])
        lines.append(Line(f"outside {osp}", max(o["price"] * qty, lot_min) / qty,
                          f"max(${o['price']}/part ({o['source']}) x {qty}, lot minimum ${lot_min:g}) / {qty}"))  # fmt: skip
    overhead = base * float(pol["overhead_pct"]) / 100.0
    lines.append(Line("overhead", overhead, f"{pol['overhead_pct']} % of material, setup, run, inspection, documents (policy)"))
    unit_cost = sum(ln.amount for ln in lines)
    tier = ctx.customer.get("tier") or pol["default_tier"]
    m = margin_pct(pol, tier, qty)
    applied = []
    for r in ctx.rules:
        if r["effect"]["type"] == "add_percent" and _rule_applies(r, part, ctx.customer):
            m += float(r["effect"]["value"])
            applied.append(f"{r['id']} (+{r['effect']['value']} %, approved by {r['approved_by']}): {r['summary']}")
    price = round(unit_cost * (1 + m / 100.0), 2)
    lines.append(Line("margin", price - unit_cost, f"{m:.1f} % = tier {tier} policy (steps at quantity, min "
                      f"{pol['min_margin_pct']} %)" + "".join(f" + {a}" for a in applied)))  # fmt: skip
    return Estimate(qty, round(unit_cost, 4), price, m, lines, minutes, size, round(kg, 4), applied)


def part_from_spec(fields: dict[str, Any], family: str | None = None) -> Part | None:
    """S4 PartSpec fields (value only) -> costing Part. None when something costing needs is missing."""
    fam = family or standards.family_from_title(fields.get("title"))
    env, mat = fields.get("envelope_mm"), fields.get("material_code")
    if not fam or not env or not mat:
        return None
    thread = str(fields.get("thread_size") or "")
    return Part(fam, mat, float(env[0]), float(env[1]), float(env[2]), int(fields.get("holes_count") or 0),
                int(fields.get("threads_count") or 0), int(fields.get("pockets") or 0), int(fields.get("bends") or 0),
                float(fields.get("tightest_tolerance_mm") or 0.1), float(fields.get("ra_um") or 3.2),
                list(fields.get("outside_processes") or []), fields.get("step_volume_cm3"),
                float(fields.get("hole_dia_mm") or (re.findall(r"M(\d+)", thread) or [6])[0]))  # fmt: skip
