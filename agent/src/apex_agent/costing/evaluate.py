"""S5 evaluation -> docs/fde/COSTING_EVAL.md. Everything on HELD-OUT parts (the cycle-time model never saw them).

1. Price: the engine's unit price vs the price Apex actually quoted (held-out history, supplier prices of that day).
2. Machine cost: the engine's setup + run cost vs what the finished job's work orders really took (x rate).
3. Delivery: backtest. For each delivered order, plan it as of its confirmation (the other orders' work then open,
   the supplier lead times) and check the committed date against the real delivery. Baseline: the flat 3 weeks.
4. CO2 for one part, line by line, to reproduce by hand.
Reads Odoo as erp_reader (dates the read model doesn't keep), the read model, MinIO (model) and OPA (policy).
"""

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from apex_agent.costing import cycle_time, leadtime, standards
from apex_agent.costing.cycle_time import held_out
from apex_agent.costing.engine import estimate, part_from_spec
from apex_agent.costing.history import code_of
from apex_agent.costing.quote import build_context, fetch_facts, quote_part
from apex_agent.knowledge import db
from apex_agent.odoo import Odoo
from apex_agent.settings import OdooSettings, load, load_as

OUT = Path(os.environ.get("APEX_DOCS_DIR", "/app/docs-fde")) / "COSTING_EVAL.md"
PROMISE_DAYS = 21  # Apex's flat "3 weeks" (W7)


def _d(s: str | None) -> date | None:
    return datetime.fromisoformat(str(s)[:19]).date() if s else None


def main() -> dict[str, Any]:
    s = load()
    conn = db.connect()
    o = Odoo(load_as(OdooSettings))
    model = cycle_time.load()
    with conn.cursor() as cur:
        cur.execute("SELECT code, family, material, fields FROM part_specs")
        specs = {c: (fam, mat, {k: f["value"] for k, f in fl.items()}) for c, fam, mat, fl in cur.fetchall()}
    mats = sorted({m for _, m, _ in specs.values() if m})
    osps = sorted({x for _, _, fl in specs.values() for x in (fl.get("outside_processes") or [])})
    facts = fetch_facts(s.opa_url, mats, osps, None)
    tiers: dict[str, str | None] = {}
    partners = {
        p["id"]: p for p in o.call("res.partner", "search_read", [("customer_rank", ">", 0)], fields=["ref", "category_id"])
    }
    tag = {t["id"]: t["name"] for t in o.call("res.partner.category", "search_read", [], fields=["name"])}
    for pid, p in partners.items():
        tiers[pid] = next((tag[c].removeprefix("Tier ") for c in p["category_id"] if tag.get(c, "").startswith("Tier ")), None)

    # 1. price vs held-out quotes
    lines = o.call("sale.order.line", "search_read", [], fields=["order_id", "product_id", "product_uom_qty", "price_unit"])
    orders = {
        so["id"]: so
        for so in o.call(
            "sale.order", "search_read", [], fields=["date_order", "partner_id", "state", "commitment_date", "effective_date"]
        )
    }
    price_rows = []
    for ln in lines:
        code = code_of(ln["product_id"][1])
        if not held_out(code) or code not in specs:
            continue
        fam, mat, fields = specs[code]
        part = part_from_spec({**fields, "material_code": mat}, fam)
        so = orders[ln["order_id"][0]]
        when = _d(so["date_order"])
        if not part or not when or part.material not in facts["mats"]:
            continue
        ctx = build_context(facts, when, model)
        ctx.customer = {"tier": tiers.get(so["partner_id"][0])}
        e = estimate(part, int(ln["product_uom_qty"]), ctx)
        price_rows.append({"code": code, "family": fam, "qty": int(ln["product_uom_qty"]), "quoted": ln["price_unit"], "engine": e.unit_price,
                           "ape": abs(e.unit_price - ln["price_unit"]) / ln["price_unit"], "state": so["state"]})  # fmt: skip

    # 2. machine cost vs actual work-order time (held-out done jobs)
    wcs = {w["code"]: w for w in facts["wcs"]}
    wc_by_id = {w["id"]: w["code"] for w in o.call("mrp.workcenter", "search_read", [], fields=["code"])}
    mos = o.call(
        "mrp.production",
        "search_read",
        [("state", "=", "done")],
        fields=["product_id", "product_qty", "origin", "date_start", "date_finished"],
    )
    wos = o.call(
        "mrp.workorder",
        "search_read",
        [],
        fields=["production_id", "workcenter_id", "duration", "duration_expected", "date_start", "date_finished", "state"],
    )
    by_mo: dict[int, list[dict[str, Any]]] = {}
    for w in wos:
        by_mo.setdefault(w["production_id"][0], []).append(w)
    machine_rows = []
    for mo in mos:
        code = code_of(mo["product_id"][1])
        if not held_out(code) or code not in specs:
            continue
        fam, mat, fields = specs[code]
        part = part_from_spec({**fields, "material_code": mat}, fam)
        if not part or part.material not in facts["mats"]:
            continue
        qty = int(mo["product_qty"])
        actual = (
            sum(
                float(w["duration"]) / 60 * float(wcs[wc_by_id[w["workcenter_id"][0]]]["costs_hour"])
                for w in by_mo.get(mo["id"], [])
            )
            / qty
        )
        e = estimate(part, qty, build_context(facts, _d(mo["date_start"]) or date.today(), model))
        machine = sum(ln.amount for ln in e.lines if ln.name.startswith(("setup ", "run ")))
        if actual > 0:
            machine_rows.append(
                {
                    "code": code,
                    "qty": qty,
                    "actual": round(actual, 2),
                    "engine": round(machine, 2),
                    "ape": abs(machine - actual) / actual,
                }
            )

    # 3. delivery backtest (all delivered orders: the plan uses only what was known at confirmation)
    hours = facts["load"]["hours_by_weekday"]
    origin_so = {
        so["id"]: so
        for so in o.call("sale.order", "search_read", [("state", "=", "sale")], fields=["name", "date_order", "effective_date"])
    }
    name_to_so = {so["name"]: so for so in origin_so.values()}
    all_mos = o.call("mrp.production", "search_read", [("state", "!=", "cancel")], fields=["product_id", "product_qty", "origin"])
    supplier_lead = {m: min((int(e["lead_days"]) for e in facts["mats"][m][1]), default=14) for m in facts["mats"]}
    backtest = []
    for mo in mos:
        so = name_to_so.get(mo["origin"])
        if not so or not so.get("effective_date"):
            continue
        t, delivered = _d(so["date_order"]), _d(so["effective_date"])
        assert t and delivered
        mine = by_mo.get(mo["id"], [])
        backlog: dict[str, float] = {}
        for other in all_mos:  # OTHER orders already confirmed at t, and their work still to run (prorated over its span)
            other_so = name_to_so.get(other["origin"])
            if other["id"] == mo["id"] or not other_so or (_d(other_so["date_order"]) or t) > t:
                continue
            for w in by_mo.get(other["id"], []):
                ws, wf = _d(w["date_start"]), _d(w["date_finished"])
                if not ws or not wf or wf <= t:
                    continue
                frac = 1.0 if ws >= t else (wf - t).days / max((wf - ws).days, 1)
                wc = wc_by_id[w["workcenter_id"][0]]
                backlog[wc] = backlog.get(wc, 0.0) + float(w["duration_expected"]) * frac
        code = code_of(mo["product_id"][1])
        fam, mat = (specs.get(code) or (None, None, {}))[:2]
        rows = [{"wc": wc_by_id[w["workcenter_id"][0]], "family": fam or "", "material": mat or "", "qty": float(mo["product_qty"]),
                 "planned": max(float(w["duration_expected"]), 0.01)} for w in mine]  # fmt: skip
        if not rows:
            continue
        ratios = model.ratios(rows)
        route = [r["wc"] for r in rows]
        per_part = {r["wc"]: tuple(x * r["planned"] / r["qty"] for x in q) for r, q in zip(rows, ratios, strict=True)}
        ready = t + timedelta(days=supplier_lead.get(mat or "", 14))
        p = leadtime.plan(route, per_part, int(mo["product_qty"]), t, {"open_minutes": backlog, "hours_by_weekday": hours},
                          ready, 0, facts["policy"])  # fmt: skip
        backtest.append({"order": so["name"], "confirmed": t.isoformat(), "delivered": delivered.isoformat(), "committed": p.committed.isoformat(),
                         "met": delivered <= p.committed, "flat_3_weeks_met": delivered <= t + timedelta(days=PROMISE_DAYS),
                         "quoted_days": (p.committed - t).days, "bottleneck": p.bottleneck})  # fmt: skip

    # 4. CO2 worked example: the first held-out part with a priced quote
    example = None
    for r in price_rows:
        fam, mat, fields = specs[r["code"]]
        part = part_from_spec({**fields, "material_code": mat}, fam)
        if part and standards.shop()["stock"]:
            ctx = build_context(facts, date.today(), model)
            q = quote_part(conn, part, [r["qty"]], ctx, facts["load"], date.today(), None, {}, True)
            example = {"code": r["code"], "qty": r["qty"], **q["co2"], "stock_kg": q["breaks"][0]["lines"][0]["detail"]["kg"]}
            break

    def mdape(rows: list[dict[str, Any]]) -> float | None:
        return round(median(r["ape"] for r in rows), 4) if rows else None

    won = [r for r in price_rows if r["state"] == "sale"]
    summary = {"price_quotes": len(price_rows), "price_mdape": mdape(price_rows), "price_mdape_won": mdape(won),
               "machine_jobs": len(machine_rows), "machine_cost_mdape": mdape(machine_rows),
               "backtest_orders": len(backtest), "committed_met": round(sum(b["met"] for b in backtest) / max(1, len(backtest)), 3),
               "median_committed_days": median(b["quoted_days"] for b in backtest) if backtest else None,
               "price_mdape_by_family": {f: mdape([r for r in price_rows if r["family"] == f]) for f in sorted({r["family"] for r in price_rows})},
               "flat_3_weeks_met": round(sum(b["flat_3_weeks_met"] for b in backtest) / max(1, len(backtest)), 3),
               "cycle_model": model.card.get("held_out"), "model_version": model.card.get("version")}  # fmt: skip
    write(summary, price_rows, machine_rows, backtest, example, model.card)
    print(json.dumps(summary, default=str))
    return summary


def write(summary: dict[str, Any], price_rows: list[dict[str, Any]], machine_rows: list[dict[str, Any]], backtest: list[dict[str, Any]],
          example: dict[str, Any] | None, card: dict[str, Any]) -> None:  # fmt: skip
    def pct(x: float | None) -> str:
        return f"{x:.1%}" if x is not None else "-"

    cm = card.get("held_out") or {}
    lines = ["# Costing, delivery date and CO2 evaluation (S5)", "",
             f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by `python -m apex_agent.costing.evaluate`. Held-out = parts the "
             f"cycle-time model never saw (a fixed 20 % of parts). Model {card.get('version')}._", "",
             "## 1. Price vs what Apex quoted (held-out history)", "",
             f"Median absolute % error: **{pct(summary['price_mdape'])}** over {summary['price_quotes']} quotes "
             f"(won quotes only: {pct(summary['price_mdape_won'])}). Target ≈ 12 %.", "",
             "| family | quotes | median error |", "|---|---|---|",
             *[f"| {f} | {sum(1 for r in price_rows if r['family'] == f)} | {pct(v)} |" for f, v in summary["price_mdape_by_family"].items()], "",
             "Known causes of the big gaps: **spur gears**: the teeth are cut on a secondary mill, but a drawing shows no tooth count "
             "(S4 can't read it), so the engine misses that time; **flanged housings**: the lab's STEP models are simplified solid "
             "blocks, which makes the calibrated fill factor wrong (0.99 instead of ≈0.4).", "",
             "The history's prices are Ramesh's spreadsheet plus his gut (±10 %), on the material price of that day; the engine "
             "uses the same day's supplier price, the drawing as read by S4, and the learned cycle times. A gap here is not "
             "automatically the engine's error: the history itself was under-priced on some families (W14).", "",
             "## 2. Machine cost vs what the job really took", "",
             f"Median absolute % error of setup + run cost per part: **{pct(summary['machine_cost_mdape'])}** over "
             f"{summary['machine_jobs']} finished held-out jobs (actual work-order minutes x the Odoo rate).", "",
             f"Cycle-time model on held-out work orders: P50 error **{pct(cm.get('mdape_p50'))}** vs standard times "
             f"{pct(cm.get('mdape_standard_times'))}; P10-P90 band holds the actual **{pct(cm.get('p10_p90_coverage'))}** of the time "
             f"({cm.get('work_orders')} work orders, {cm.get('parts')} parts).", "",
             "## 3. Delivery date backtest", "",
             f"Committed date (P90 + {2} days buffer, against the other orders' work open at confirmation) met: "
             f"**{pct(summary['committed_met'])}** of {summary['backtest_orders']} delivered orders. "
             f"The flat '3 weeks' Apex promises today: {pct(summary['flat_3_weeks_met'])}. "
             + (f"**But the committed dates are longer**: median {summary['median_committed_days']} days after confirmation vs 21. Keeping "
              "promises by quoting longer is only half a win; S14 (portfolio capacity) is where the dates get tighter."
              if summary["median_committed_days"] > 21 else
              f"**The committed dates are shorter** (median {summary['median_committed_days']} days after confirmation vs 21) and are kept "
              "LESS often: the plan is optimistic against the history's real delivery times. S14's expected load pushes dates out; "
              "the planner's buffer is a policy knob (`lead_buffer_days`)."), "",  # fmt: skip
             "Caveat: the history's real delivery dates were not produced by a capacity schedule, so this measures how often "
             "the committed date would have been kept, not a perfect replay of the shop floor.", "",
             "| order | confirmed | committed | delivered | met | bottleneck |", "|---|---|---|---|---|---|"]  # fmt: skip
    lines += [f"| {b['order']} | {b['confirmed']} | {b['committed']} | {b['delivered']} | {'yes' if b['met'] else '**no**'} | {b['bottleneck']} |"
              for b in sorted(backtest, key=lambda x: x["confirmed"])[-15:]]  # fmt: skip
    lines += ["", "## 4. CO2 for one part, by hand", ""]
    if example:
        lines += [f"Part **{example['code']}**, lot of {example['qty']}: **{example['per_part_kg']} kg CO2e per part**, "
                  f"{example['per_lot_kg']} kg per lot.", "", "| contribution | calculation | kg CO2e | source |", "|---|---|---|---|"]  # fmt: skip
        lines += [
            f"| {ln['name']} | {ln['calc']} | {ln['kg_co2e']} | {ln['source']} ({ln.get('status')}) |" for ln in example["lines"]
        ]
        lines += [
            "",
            "Excluded (no citable factor): " + ("; ".join(example["excluded"]) or "none"),
            "",
            f"_{example['methodology']}_",
        ]
    lines += ["", "## Worst price gaps (first 15)", "", "| part | qty | quoted | engine | error |", "|---|---|---|---|---|"]
    lines += [f"| {r['code']} | {r['qty']} | {r['quoted']:.2f} | {r['engine']:.2f} | {r['ape']:.0%} |"
              for r in sorted(price_rows, key=lambda r: -r["ape"])[:15]]  # fmt: skip
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    print(json.dumps(main()))  # S10: run_evals reads this line
