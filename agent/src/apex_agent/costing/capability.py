"""Can Apex make this part? (S11, W11: quoting what we can't make). Deterministic: no LLM.

For each machining step of the part's routing, at least ONE machine that can do that step must fit the part (its X/Y/Z
travel, 0 = no limit) and hold its tightest tolerance and its finest finish (Odoo's work-center fields, the same facts
costing uses). Materials, outside processes and certificates come next: a missing one is a CONDITION, not a no-bid.

    bid                   everything fits
    bid_with_conditions   Apex can make it if a condition is met (a material or a process with no supplier price, a
                          certificate Apex doesn't hold): quoted, with the conditions in the reply
    no_bid                no machine can do a step: not costed; a polite decline goes to a person for approval

Usage (knowledge job): python -m apex_agent.costing.capability eval   # every extracted RFQ vs the labelled ones
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

from apex_agent.costing import standards
from apex_agent.costing.standards import Part

# a routing step -> the machines that can do it (the 5-axis mill can do any 3-axis job, tighter)
CAN_DO = {"MILL3": ["MILL3", "MILL5"], "MILL5": ["MILL5"], "LATHE": ["LATHE"], "LASER": ["LASER"], "BRAKE": ["BRAKE"]}


MACHINING = {"MILL3", "MILL5", "LATHE"}  # surface finish is a machined property; a sheet's surface is the material's


def too_big(part: Part, wc: dict[str, Any]) -> str | None:
    travel = sorted((float(wc.get(k) or 0) for k in ("x_max_x_mm", "x_max_y_mm", "x_max_z_mm")), reverse=True)
    size = sorted((part.length, part.width, part.height), reverse=True)
    if any(t and s > t for s, t in zip(size, travel, strict=True)):
        return f"{max(size):.0f} mm is beyond {wc['code']}'s travel ({' x '.join(f'{t:.0f}' for t in travel if t)} mm)"
    return None


def check(part: Part, wcs: list[dict[str, Any]], *, material_known: bool = True, outside_priced: dict[str, bool] | None = None,
          certificates: list[str] | None = None, held: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    """Size per step (some machine that can do the step must fit the part); tolerance and finish against the BEST machine
    in the routing (the critical feature is finished on the machine that holds it, e.g. a shaft's fit on the 5-axis mill)."""
    by_code = {w["code"]: w for w in wcs}
    reasons: list[str] = []
    capable: list[dict[str, Any]] = []
    for step in standards.routing(part):
        options = [by_code[m] for m in CAN_DO.get(step, []) if m in by_code]
        if not options:
            continue  # saw, deburr, inspection: not the limiting steps
        fitting = [m for m in options if not too_big(part, m)]
        if not fitting:
            reasons.append(str(too_big(part, options[-1])))  # the biggest machine for that step, and why even it can't
        capable += fitting or options
    # Tolerance and finish: the routing's machines, else a SECONDARY operation on the shop's best machining center
    # (a condition: it isn't in the standard routing, so a person adds it to the cost), else no machine at all: no-bid.
    shop = [w for w in wcs if w["code"] in MACHINING]
    conditions: list[str] = []
    for label, value, field, machines, unit in (
        ("tolerance", part.tolerance_mm, "x_best_tolerance_mm", capable, "mm"),
        ("surface Ra", part.ra_um, "x_best_ra", [m for m in capable if m["code"] in MACHINING] or capable, "µm"),
    ):
        if label == "surface Ra" and not any(m["code"] in MACHINING for m in capable):
            continue  # a sheet's surface is the material's, not the cut's
        best = min((float(m.get(field) or 0) for m in machines if float(m.get(field) or 0)), default=0.0)
        shop_best = min((float(m.get(field) or 0) for m in shop if float(m.get(field) or 0)), default=0.0)
        if best and value < best:
            if shop_best and value >= shop_best:
                best_wc = min((m for m in shop if float(m.get(field) or 0)), key=lambda m: float(m.get(field) or 0))["code"]
                conditions.append(f"{label} {value} {unit} needs a secondary operation on {best_wc} (not in the standard routing): "
                                  "a person adds it to the cost")  # fmt: skip
            else:
                reasons.append(f"{label} {value} {unit} is finer than any machine in the shop holds ({shop_best or best} {unit})")
    if reasons:
        return {"decision": "no_bid", "reasons": reasons, "conditions": []}
    if not material_known:
        conditions.append(
            f"material {part.material} is not in our stock list: price and lead time to be confirmed by the supplier"
        )
    conditions += [
        f"outside process {o}: no current supplier price; to be confirmed" for o, ok in (outside_priced or {}).items() if not ok
    ]
    held_l = [h.lower() for h in held or []]
    conditions += [f"certificate {c}: not one Apex holds; available through a certified partner at extra cost" for c in certificates or []
                   if not any(h in c.lower() for h in held_l)]  # fmt: skip
    return {"decision": "bid_with_conditions" if conditions else "bid", "reasons": [], "conditions": conditions}


def evaluate() -> dict[str, Any]:
    """Every extracted labelled RFQ part, checked against the live machines: are the 8 out-of-capability parts caught,
    and is no capable part refused? Reads stored extractions (no LLM)."""
    from apex_agent.costing.engine import part_from_spec
    from apex_agent.costing.quote import fetch_facts
    from apex_agent.knowledge import db

    labels = {
        r["part_code"]: r
        for r in json.loads((Path(os.environ.get("APEX_EVAL_DIR", "/eval")) / "rfq_parts.json").read_text(encoding="utf-8"))
    }
    wcs = fetch_facts(os.environ.get("APEX_OPA_URL", "http://opa:8181"), [], [], None)["wcs"]
    caught: list[dict[str, Any]] = []
    missed: list[dict[str, Any]] = []
    false_no: list[dict[str, Any]] = []
    checked = 0
    with db.connect().cursor() as cur:
        cur.execute(
            # one extraction per part: the most complete (an RFQ sent twice may be read with a field missing), then the
            # oldest; deterministic, so the score doesn't change with the row order
            """SELECT DISTINCT ON (p->>'part_code') p FROM extractions x, jsonb_array_elements(x.result->'parts') p
               ORDER BY p->>'part_code', (SELECT count(*) FROM jsonb_each(p->'fields') f WHERE f.value->'value' <> 'null') DESC,
                        x.email_id"""
        )
        for (p,) in cur.fetchall():
            lab = labels.get(p["part_code"])
            if not lab:
                continue
            part = part_from_spec({k: f["value"] for k, f in p["fields"].items()}, None)
            if not part:
                if lab["kind"] == "out_of_capability":  # never drop a labelled no-bid silently: it counts as missed
                    missed.append({"part": p["part_code"], "label": lab["reason"], "got": "spec incomplete: not checkable"})
                continue
            checked += 1
            v = check(part, wcs)
            if lab["kind"] == "out_of_capability":
                (caught if v["decision"] == "no_bid" else missed).append(
                    {"part": p["part_code"], "label": lab["reason"], "got": v}
                )
            elif v["decision"] == "no_bid":
                false_no.append({"part": p["part_code"], "kind": lab["kind"], "why": v["reasons"]})
    return {"parts_checked": checked, "out_of_capability_caught": len(caught), "out_of_capability_missed": missed,
            "capable_refused": false_no, "examples": [{"part": c["part"], "label": c["label"], "why": c["got"]["reasons"]} for c in caught]}  # fmt: skip


if __name__ == "__main__":
    if sys.argv[1:] != ["eval"]:
        raise SystemExit(__doc__)
    res = evaluate()
    out = Path(os.environ.get("APEX_DOCS_DIR", "/app/docs-fde")) / "CAPABILITY_EVAL.md"
    lines = ["# Capability check (S11): can Apex make it?", "",
             "_Generated by `python -m apex_agent.costing.capability eval` from the stored extractions and the live machine data "
             "in Odoo, against the labelled RFQ parts (L3). No LLM._", "",
             f"- Parts checked: **{res['parts_checked']}**",
             f"- Out-of-capability parts caught (no-bid): **{res['out_of_capability_caught']} of {res['out_of_capability_caught'] + len(res['out_of_capability_missed'])}**",
             f"- Capable parts refused: **{len(res['capable_refused'])}**", "",
             "| Part | Label (why it can't be made) | The check's reason |", "|---|---|---|",
             *[f"| {x['part']} | {x['label']} | {'; '.join(x['why'])} |" for x in res["examples"]], ""]  # fmt: skip
    out.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False))
