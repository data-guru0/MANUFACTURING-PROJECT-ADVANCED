"""DFM suggestions (S13, W12: no cost-down advice). Deterministic: no LLM.

The rules live in policy.yaml -> dfm (read through OPA). For each rule a part triggers, the engine prices the part again
with ONLY that one feature changed; the saving is the difference. The customer's spec is never changed: a suggestion is
an OPTIONAL alternate line on the quote, offered only when the engine's saving reaches `min_saving_pct`.

    tolerance_tighter_than_needed  tighter than the family usually needs -> the family's tolerance
    finish_finer_than_needed       finer Ra than the family usually needs -> the family's Ra
    exotic_material                a material with a standard alternative (e.g. 316 -> 304 stainless)
    non_standard_threads           a thread outside the shop's standard taps: advice only (the engine has no cost for it)
    deep_holes / small_internal_radii / thin_walls   defined in policy, but NOT checkable: the drawings' extracted data
                                   has no hole depth, internal radius or wall thickness (S4). Reported as not checked.

Usage (knowledge job): python -m apex_agent.costing.dfm eval   # labelled RFQs (ground truth) vs what the agent read
"""

import dataclasses
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from apex_agent.costing.engine import Context, estimate
from apex_agent.costing.standards import Part

PRICED = ("tolerance_tighter_than_needed", "finish_finer_than_needed", "exotic_material")


def issues(part: Part, thread_size: str | None, rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Which rules the part triggers, and the one-field change each one suggests (no pricing)."""
    out: list[dict[str, Any]] = []
    tol = (rules.get("tolerance_needed_mm") or {}).get(part.family)
    if tol and part.tolerance_mm < tol:
        out.append({"rule": "tolerance_tighter_than_needed", "field": "tolerance_mm", "old": part.tolerance_mm, "new": tol,
                    "why": f"±{part.tolerance_mm} mm is tighter than a {part.family.replace('_', ' ')} usually needs (±{tol} mm); "
                           "if the function allows, the looser tolerance avoids slower machining and inspection"})  # fmt: skip
    ra = (rules.get("ra_needed_um") or {}).get(part.family)
    if ra and part.ra_um < ra and part.process != "sheet":
        out.append({"rule": "finish_finer_than_needed", "field": "ra_um", "old": part.ra_um, "new": ra,
                    "why": f"Ra {part.ra_um} µm is finer than a {part.family.replace('_', ' ')} usually needs (Ra {ra} µm)"})  # fmt: skip
    alt = (rules.get("material_alternatives") or {}).get(part.material)
    if alt:
        out.append({"rule": "exotic_material", "field": "material", "old": part.material, "new": alt["to"],
                    "why": f"{part.material} -> {alt['to']}: {alt['note']}"})  # fmt: skip
    std = rules.get("standard_threads") or []
    if part.threads and thread_size and std and thread_size.replace(" ", "") not in std:
        out.append({"rule": "non_standard_threads", "field": "thread_size", "old": thread_size, "new": None,
                    "why": f"{thread_size} is not one of the shop's standard taps ({', '.join(std)}): special tooling; "
                           "a standard metric coarse thread is usually cheaper and faster"})  # fmt: skip
    return out


def suggest(part: Part, qty: int, ctx: Context, docs: dict[str, bool], thread_size: str | None, rules: dict[str, Any],
            base_unit_price: float) -> dict[str, Any]:  # fmt: skip
    """Each issue priced by the engine with only its field changed. `offered`: becomes an optional alternate line."""
    out = []
    for i in issues(part, thread_size, rules):
        s = {**i, "offered": False}
        if i["rule"] in PRICED:
            alt = dataclasses.replace(part, **{i["field"]: i["new"]})
            try:
                e = estimate(alt, qty, ctx, docs)
            except KeyError as ex:  # e.g. the alternative material has no price in today's facts
                s["not_priced"] = f"{ex} is not in today's facts"
            else:
                s.update(qty=qty, alt_unit_price=e.unit_price, saving_per_unit=round(base_unit_price - e.unit_price, 2),
                         saving_pct=round(100 * (base_unit_price - e.unit_price) / base_unit_price, 1),
                         changed_lines={ln.name: round(ln.amount, 2) for ln in e.lines})  # fmt: skip
                s["offered"] = s["saving_pct"] >= float(rules.get("min_saving_pct", 3))
        else:
            s["not_priced"] = "advice only: the engine has no cost for this feature"
        out.append(s)
    return {"suggestions": out, "not_checked": rules.get("not_checkable", [])}


def option_label(code: str, s: dict[str, Any]) -> str:
    """The optional alternate line's text (Odoo option and reply)."""
    unit = {"tolerance_mm": " mm", "ra_um": " µm Ra"}.get(s["field"], "")
    return f"[{code}] OPTIONAL design alternative: {s['field']} {s['old']}{unit} -> {s['new']}{unit} (saves {s['saving_pct']} %)"


# ---- evaluation (no LLM)
# The generator draws an inch part's thread as its UNC equivalent (legacy/seed/drawings.py INCH_THREAD): the label must
# use what is ON THE DRAWING, not the generator's internal metric size.
INCH_THREAD = {
    "M4x0.7": "#8-32 UNC",
    "M5x0.8": "#10-24 UNC",
    "M6x1.0": "1/4-20 UNC",
    "M8x1.25": "5/16-18 UNC",
    "M10x1.5": "3/8-16 UNC",
}


def _gt_part(g: dict[str, Any]) -> tuple[Part, str]:
    env = g["envelope_mm"]
    size = g["threads"]["size"]
    return Part(g["family"], g["material"], float(env[0]), float(env[1]), float(env[2]), int(g["holes"]["count"]),
                int(g["threads"]["count"]), int(g["pockets"]), int(g["bends"]), float(g["tightest_tolerance_mm"]),
                float(g["ra_um"]), list(g["outside_processes"]), g.get("step_volume_cm3"),
                float(g["holes"]["dia_mm"] or 6)), (INCH_THREAD.get(size, size) if g["units"] == "inch" else size)  # fmt: skip


def evaluate() -> dict[str, Any]:
    """Labels: the rules applied to the GENERATOR's ground truth of every capable RFQ part (what is really on the drawing).
    Found: the rules applied to what the agent EXTRACTED. Then every priced suggestion is reproduced: a fresh engine run
    of the alternative spec, in a fresh context, must give the same saving to the cent, and differ in one field only."""
    from apex_agent import opa
    from apex_agent.costing import cycle_time
    from apex_agent.costing.engine import part_from_spec
    from apex_agent.costing.quote import build_context, fetch_facts
    from apex_agent.costing.repeat import approved_spec
    from apex_agent.knowledge import db

    opa_url = os.environ.get("APEX_OPA_URL", "http://opa:8181")
    rules = opa.decide(opa_url, "policy/dfm", {}) or {}
    ev = Path(os.environ.get("APEX_EVAL_DIR", "/eval"))
    gts = {g["part_code"]: g for g in (json.loads(p.read_text(encoding="utf-8")) for p in sorted((ev / "ground_truth").glob("RFQ-*.json")))
           if g["kind"] != "out_of_capability"}  # fmt: skip
    conn = db.connect()
    with conn.cursor() as cur:  # the most complete extraction of each part (as in the S11 eval)
        cur.execute("""SELECT DISTINCT ON (p->>'part_code') p FROM extractions x, jsonb_array_elements(x.result->'parts') p
                       ORDER BY p->>'part_code', (SELECT count(*) FROM jsonb_each(p->'fields') f WHERE f.value->'value' <> 'null') DESC,
                                x.email_id""")  # fmt: skip
        extracted = {p["part_code"]: p for (p,) in cur.fetchall()}
    mats = sorted({g["material"] for g in gts.values()} | {a["to"] for a in (rules.get("material_alternatives") or {}).values()})
    osps = sorted({o for g in gts.values() for o in g["outside_processes"]})
    model = cycle_time.load()
    on = date.today()
    ctx = build_context(fetch_facts(opa_url, mats, osps, None), on, model)
    ctx2 = build_context(fetch_facts(opa_url, mats, osps, None), on, model)  # a second, independent fetch
    res: dict[str, Any] = {"labelled_issues": 0, "found": 0, "missed": [], "false_issues": [], "by_rule": {}, "parts": 0,
                           "not_costed": [], "priced": 0, "offered": 0, "reproduced": 0, "not_reproduced": [],
                           "spec_changed_elsewhere": [], "examples": []}  # fmt: skip
    for code, g in gts.items():
        x = extracted.get(code) or {}
        gt_part, gt_thread = _gt_part(g)
        want = {i["rule"] for i in issues(gt_part, gt_thread, rules)}
        fields = {k: f["value"] for k, f in (x.get("fields") or {}).items()}
        family = None
        if g["kind"] == "repeat":  # as in the graph (S12): a repeat is costed from the archived approved spec
            family, fields = approved_spec(conn, g["source_part"])
        part = part_from_spec(fields, family)
        if not part:  # no quote without a complete spec: the RFQ goes to clarification first (S4), DFM waits for it
            res["not_costed"].append({"part": code, "issues_waiting": sorted(want)})
            continue
        res["labelled_issues"] += len(want)
        res["parts"] += 1
        thread = fields.get("thread_size")
        qty = int((x.get("quantities") or [100])[0])
        base = estimate(part, qty, ctx, {})
        got = suggest(part, qty, ctx, {}, thread, rules, base.unit_price)["suggestions"]
        names = {s["rule"] for s in got}
        res["found"] += len(want & names)
        res["missed"] += [{"part": code, "rule": r} for r in sorted(want - names)]
        res["false_issues"] += [{"part": code, "rule": r} for r in sorted(names - want)]
        for r in want:
            res["by_rule"].setdefault(r, [0, 0])
            res["by_rule"][r][1] += 1
            res["by_rule"][r][0] += r in names
        for s in got:
            if "saving_per_unit" not in s:
                continue
            res["priced"] += 1
            res["offered"] += s["offered"]
            alt = dataclasses.replace(part, **{s["field"]: s["new"]})
            diff = [f.name for f in dataclasses.fields(Part) if getattr(alt, f.name) != getattr(part, f.name)]
            if diff != [s["field"]]:
                res["spec_changed_elsewhere"].append({"part": code, "rule": s["rule"], "fields": diff})
            again = round(estimate(part, qty, ctx2, {}).unit_price - estimate(alt, qty, ctx2, {}).unit_price, 2)
            if again == s["saving_per_unit"]:
                res["reproduced"] += 1
            else:
                res["not_reproduced"].append({"part": code, "rule": s["rule"], "saving": s["saving_per_unit"], "again": again})
            if s["offered"] and len(res["examples"]) < 12:
                res["examples"].append({"part": code, "qty": qty, "rule": s["rule"], "change": f"{s['old']} -> {s['new']}",
                                        "price": base.unit_price, "alt": s["alt_unit_price"], "saving_pct": s["saving_pct"]})  # fmt: skip
    return res


if __name__ == "__main__":
    if sys.argv[1:] != ["eval"]:
        raise SystemExit(__doc__)
    r = evaluate()
    out = Path(os.environ.get("APEX_DOCS_DIR", "/app/docs-fde")) / "DFM_EVAL.md"
    lines = ["# DFM suggestions (S13)", "",
             "_Generated by `python -m apex_agent.costing.dfm eval`. Labels: the policy's DFM rules applied to the generator's "
             "ground truth of each capable RFQ part (what is really on the drawing). Found: the same rules applied to what the "
             "agent extracted. Every saving is recomputed by the engine in an independent context. No LLM._", "",
             f"- Parts that get a quote: **{r['parts']}**. Not costed (clarification first, so no DFM yet): {len(r['not_costed'])} "
             f"parts with {sum(len(n['issues_waiting']) for n in r['not_costed'])} labelled issues waiting",
             f"- Labelled DFM issues found: **{r['found']} of {r['labelled_issues']}**; false issues: **{len(r['false_issues'])}**",
             f"- Savings priced by the engine: **{r['priced']}**, reproduced to the cent: **{r['reproduced']}**; "
             f"offered as optional lines (saving >= policy minimum): **{r['offered']}**",
             f"- Suggestions that changed any other field of the customer's spec: **{len(r['spec_changed_elsewhere'])}** (must be 0)",
             "- Not checkable (no data in the drawings' extraction): deep holes, small internal radii, thin walls", "",
             "| Rule | Found / labelled |", "|---|---|", *[f"| {k} | {v[0]} / {v[1]} |" for k, v in sorted(r["by_rule"].items())], "",
             "| Part | Qty | Rule | Change | Price | Alternative | Saving |", "|---|---|---|---|---|---|---|",
             *[f"| {e['part']} | {e['qty']} | {e['rule']} | {e['change']} | ${e['price']:.2f} | ${e['alt']:.2f} | {e['saving_pct']} % |"
               for e in r["examples"]], ""]  # fmt: skip
    out.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({k: v for k, v in r.items() if k != "examples"}, ensure_ascii=False, default=str))
