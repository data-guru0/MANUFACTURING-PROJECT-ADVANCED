"""Quoted versus actual (S18, W14: nobody knows which jobs lose money). Deterministic: no LLM.

From the read model (Odoo, synced by S2): every won quote whose manufacturing order is done.
    job       revenue (the quotation) vs actual cost = the job's purchase orders + every work order's real minutes x its
              machine's rate, + 15 % overhead (the same formula as L6's baseline, so the numbers can be compared)
    operation the planned minutes (Odoo's routing) vs the real ones, per work order, tagged with the part's family,
              material and the customer: where the estimates are systematically wrong
Writes `variance_jobs` and `variance_ops` (Grafana's "Quoted vs actual" dashboard reads them).

Usage (knowledge job): python -m apex_agent.costing.variance
"""

import json
from statistics import median
from typing import Any

OVERHEAD = 0.15  # = L6 (scripts/baseline_metrics.py)

# the tables (variance_jobs, variance_ops) are in ingest/schema.sql: they must exist before access is granted


def compute(conn: Any) -> dict[str, Any]:

    def q(sql: str) -> list[Any]:
        return list(conn.execute(sql).fetchall())

    orders = {d["name"]: d for (d,) in q("SELECT data FROM odoo_records WHERE model = 'sale.order' AND data->>'state' = 'sale'")}
    lines: dict[str, Any] = {}
    for (d,) in q("SELECT data FROM odoo_records WHERE model = 'sale.order.line'"):
        if d["order_id"] and d["order_id"][1] in orders and d["product_id"]:
            lines.setdefault(d["order_id"][1], d)
    mos = {i: d for i, d in q("SELECT id, data FROM odoo_records WHERE model = 'mrp.production' AND data->>'state' = 'done'")}
    done_so = {d["origin"] for d in mos.values() if d.get("origin") in orders}
    rate = {
        i: (d["code"], float(d["costs_hour"])) for i, d in q("SELECT id, data FROM odoo_records WHERE model = 'mrp.workcenter'")
    }
    buy: dict[str, float] = {}
    for (d,) in q("SELECT data FROM odoo_records WHERE model = 'purchase.order' AND data->>'state' IN ('purchase', 'done')"):
        buy[d["origin"]] = buy.get(d["origin"], 0.0) + float(d["amount_untaxed"])
    specs = {c: (fam, mat) for c, fam, mat in q("SELECT code, family, material FROM part_specs")}
    machine: dict[str, float] = {}
    planned: dict[str, float] = {}
    actual: dict[str, float] = {}
    ops = []
    for (w,) in q("SELECT data FROM odoo_records WHERE model = 'mrp.workorder'"):
        mo = mos.get(w["production_id"][0]) if w["production_id"] else None
        if not mo or mo["origin"] not in done_so:
            continue
        so = mo["origin"]
        wc, per_h = rate[w["workcenter_id"][0]]
        machine[so] = machine.get(so, 0.0) + float(w["duration"]) / 60 * per_h
        planned[so] = planned.get(so, 0.0) + float(w["duration_expected"])
        actual[so] = actual.get(so, 0.0) + float(w["duration"])
        code = (lines.get(so) or {}).get("product_id", [None, ""])[1].split("]")[0].lstrip("[")
        fam, mat = specs.get(code, (None, None))
        if float(w["duration_expected"]) > 0:
            ops.append((so, wc, fam, mat, orders[so]["partner_id"][1], float(w["duration_expected"]), float(w["duration"]),
                        round(float(w["duration"]) / float(w["duration_expected"]), 3)))  # fmt: skip
    jobs = []
    for so in sorted(done_so):
        ln = lines.get(so) or {}
        code = (ln.get("product_id") or [None, ""])[1].split("]")[0].lstrip("[")
        fam, mat = specs.get(code, (None, None))
        cost = (buy.get(so, 0.0) + machine.get(so, 0.0)) * (1 + OVERHEAD)
        revenue = float(orders[so]["amount_untaxed"])
        jobs.append((so, orders[so]["partner_id"][1], code, fam, mat, ln.get("product_uom_qty"), round(revenue, 2), round(buy.get(so, 0.0), 2),
                     round(machine.get(so, 0.0), 2), round(cost, 2), round(revenue - cost, 2), cost > revenue,
                     round(planned.get(so, 0.0), 1), round(actual.get(so, 0.0), 1)))  # fmt: skip
    with conn.cursor() as cur:
        cur.execute("TRUNCATE variance_jobs, variance_ops")
        cur.executemany("""INSERT INTO variance_jobs (sale_order, customer, part, family, material, qty, revenue, material_cost, machine_cost,
                             actual_cost, profit, lost_money, minutes_planned, minutes_actual) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", jobs)  # fmt: skip
        cur.executemany("""INSERT INTO variance_ops (sale_order, work_center, family, material, customer, minutes_planned, minutes_actual, ratio)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", ops)  # fmt: skip
    conn.commit()
    by_family: dict[str, list[float]] = {}
    for o in ops:
        if o[2]:
            by_family.setdefault(o[2], []).append(o[7])
    losers = sorted((j for j in jobs if j[11]), key=lambda j: j[10])
    return {"finished_jobs": len(jobs), "lost_money": len(losers), "total_loss_usd": round(-sum(j[10] for j in losers), 2),
            "operations": len(ops),
            "minutes_actual_vs_planned_by_family": {f: {"median_ratio": round(median(v), 3), "ops": len(v)} for f, v in sorted(by_family.items())},
            "worst": [{"so": j[0], "customer": j[1], "part": j[2], "profit": j[10]} for j in losers[:5]]}  # fmt: skip


if __name__ == "__main__":
    from apex_agent.knowledge import db

    print(json.dumps(compute(db.connect()), default=str))
