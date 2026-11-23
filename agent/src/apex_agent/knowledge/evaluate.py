"""Retrieval check (S2 Done-when): for held-out parts (the 80 future RFQ parts from L3, never in Odoo),
do the top-5 analogs contain a part of the SAME family and SAME material?

The query is what an extractor would produce from the drawing: family, the material text exactly as
written on the drawing, the envelope and the main features. Ground truth is used ONLY here, to score.
Two modes: text only, and text + the material filter (S4 extraction gives a material code).
Writes docs/fde/RETRIEVAL_EVAL.md.

Usage: python -m apex_agent.knowledge.evaluate
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from apex_agent.knowledge.db import connect
from apex_agent.knowledge.normalize import MaterialMatcher
from apex_agent.knowledge.search import search

EVAL_DIR = Path(os.environ.get("APEX_EVAL_DIR", "/eval"))
OUT = Path(os.environ.get("APEX_DOCS_DIR", "/app/docs-fde")) / "RETRIEVAL_EVAL.md"


def query_text(gt: dict[str, Any]) -> str:
    env = gt.get("envelope_mm") or []
    holes = int((gt.get("holes") or {}).get("count") or 0)
    material = gt.get("material_text_on_drawing") or ""
    return " ".join(x for x in [str(gt["family"]).replace("_", " "), material,
                                " x ".join(f"{d:g}" for d in env) + " mm" if env else "", f"{holes} holes" if holes else "",
                                f"{gt.get('bends')} bends" if gt.get("bends") else ""] if x)  # fmt: skip


def hit(results: list[dict[str, Any]], gt: dict[str, Any]) -> bool:
    return any(r["family"] == gt["family"] and r["material_code"] == gt["material"] for r in results)


def main() -> None:
    index = json.loads((EVAL_DIR / "rfq_parts.json").read_text(encoding="utf-8"))
    matcher = MaterialMatcher()
    rows, hits_text, hits_filter = [], 0, 0
    with connect() as conn:
        for item in index:
            gt = json.loads((EVAL_DIR / "ground_truth" / f"{item['rfq_id']}.json").read_text(encoding="utf-8"))
            q = query_text(gt)
            t = search(conn, q, exclude=gt["part_code"])
            mat = matcher.match(gt.get("material_text_on_drawing") or "")[0]
            f = search(conn, q, material=mat, exclude=gt["part_code"]) if mat else t
            hits_text += hit(t, gt)
            hits_filter += hit(f, gt)
            rows.append((item["rfq_id"], gt["family"], gt["material"], q, hit(t, gt), hit(f, gt), t[0]["code"] if t else "-"))
    n = len(rows)
    lines = ["# Analog retrieval check (S2)", "",
             f"_Generated {datetime.now():%Y-%m-%d %H:%M} by `apex_agent.knowledge.evaluate` on {n} held-out RFQ parts "
             "(never in Odoo). A hit = the top-5 analogs contain a part with the same family AND material._", "",
             "| Mode | Hits | Rate |", "|---|---|---|",
             f"| Text only (family + drawing material text + envelope + features) | {hits_text}/{n} | **{100 * hits_text / n:.1f} %** |",
             f"| Text + material filter (as after S4 extraction) | {hits_filter}/{n} | **{100 * hits_filter / n:.1f} %** |", "",
             "## Misses (text only)", "", "| RFQ | Family | Material | Query | Top hit |", "|---|---|---|---|---|",
             *[f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[6]} |" for r in rows if not r[4]]]  # fmt: skip
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"held_out": n, "top5_same_family_material_text": round(hits_text / n, 3),
                      "top5_same_family_material_with_filter": round(hits_filter / n, 3)}))  # fmt: skip


if __name__ == "__main__":
    main()
