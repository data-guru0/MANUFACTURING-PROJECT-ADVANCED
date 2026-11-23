"""Read Apex's OWN drawings of its history parts (Odoo attachments) with the S4 readers -> `part_specs` (S5).

This gives costing what Odoo doesn't store: each part's size, holes, threads, tolerances, finish and, from the STEP
model when there is one, its real volume. Used (1) to calibrate the fill factor (finished / envelope volume) per
family, which turns a drawing's outside size into material removed, and (2) to evaluate prices against history.
Reads as `erp_reader` (read-only). Idempotent.

Usage (knowledge job): python -m apex_agent.costing.history
"""

import base64
import hashlib
import json
import re
from statistics import median
from typing import Any

from psycopg.types.json import Json

from apex_agent.documents import readers
from apex_agent.knowledge import db
from apex_agent.knowledge.normalize import MaterialMatcher
from apex_agent.odoo import Odoo
from apex_agent.settings import OdooSettings, load_as


def code_of(display: str) -> str:
    """'[D-10005-A] Sheet-Metal Panel …' -> 'D-10005-A' (Odoo's display name of a product)."""
    m = re.match(r"\[([^\]]+)\]", display or "")
    return m.group(1) if m else ""


def read_all() -> dict[str, Any]:
    o = Odoo(load_as(OdooSettings))
    parts = o.call("product.template", "search_read", [("x_drawing_no", "!=", False)],
                   fields=["default_code", "x_part_family", "bom_ids"])  # fmt: skip
    boms = {b["id"]: b for b in o.call("mrp.bom", "read", [b for p in parts for b in p["bom_ids"][:1]], ["bom_line_ids"])}
    lines = {
        ln["id"]: ln
        for ln in o.call("mrp.bom.line", "read", [i for b in boms.values() for i in b["bom_line_ids"]], ["product_id"])
    }
    matcher = MaterialMatcher()
    conn = db.connect()
    stored = with_step = 0
    for p in parts:
        atts = o.call("ir.attachment", "search_read", [("res_model", "=", "product.template"), ("res_id", "=", p["id"])],
                      fields=["name", "datas"])  # fmt: skip
        files = {a["name"].lower().rsplit(".", 1)[-1]: (a["name"], base64.b64decode(a["datas"])) for a in atts if a.get("datas")}
        facts: dict[str, Any] = {}
        if "dxf" in files:
            facts = readers.read_dxf(files["dxf"][1], files["dxf"][0])
        elif "pdf" in files:
            facts = readers.read_pdf(files["pdf"][1], files["pdf"][0])
        if not facts:
            continue
        step_volume = None
        if "step" in files:
            s = readers.read_step(files["step"][1], files["step"][0])
            facts.update(s)
            step_volume = s.get("step_volume_cm3", {}).get("value")
            with_step += step_volume is not None
        bom = boms.get(p["bom_ids"][0]) if p["bom_ids"] else None
        mat = next((code_of(lines[i]["product_id"][1]) for i in (bom or {}).get("bom_line_ids", []) if i in lines), None)
        if not mat and facts.get("material_text", {}).get("value"):
            mat = matcher.match(str(facts["material_text"]["value"]))[0]
        env = (facts.get("envelope_mm") or {}).get("value")
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO part_specs (code, family, material, fields, envelope, step_volume, files) VALUES (%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (code) DO UPDATE SET family = EXCLUDED.family, material = EXCLUDED.material,
                             fields = EXCLUDED.fields, envelope = EXCLUDED.envelope, step_volume = EXCLUDED.step_volume,
                             files = EXCLUDED.files, read_at = now()""",
                        (p["default_code"], p["x_part_family"], mat, Json(facts), env, step_volume,
                         Json([{"name": n, "sha256": hashlib.sha256(b).hexdigest()} for n, b in files.values()])))  # fmt: skip
        stored += 1
    conn.commit()
    summary = {
        "parts_with_drawings": len(parts),
        "specs_stored": stored,
        "with_step_volume": with_step,
        "fill": fill_factors(conn),
    }
    print(json.dumps(summary))
    return summary


def fill_factors(conn: Any) -> dict[str, Any]:
    """Median finished / envelope volume per family, from parts that have a STEP model (the real volume)."""
    with conn.cursor() as cur:
        cur.execute("SELECT family, step_volume, envelope FROM part_specs WHERE step_volume IS NOT NULL AND envelope IS NOT NULL")
        rows = cur.fetchall()
    by: dict[str, list[float]] = {}
    for fam, vol, env in rows:
        box = env[0] * env[1] * env[2] / 1000.0
        if box > 0:
            by.setdefault(fam, []).append(vol / box)
    return {fam: {"fill": round(median(v), 3), "parts": len(v)} for fam, v in sorted(by.items())}


if __name__ == "__main__":
    read_all()
