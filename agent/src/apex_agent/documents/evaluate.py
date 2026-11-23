"""S4 evaluation: per-field extraction accuracy against the L3 ground truth, and what the readers get from the NIST
test drawings. The ground truth and the labels are read ONLY here (mounted at /eval), never by the worker.

Writes docs/fde/EXTRACTION_EVAL.md and prints a JSON summary.
"""

import json
import os
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apex_agent.documents import scan
from apex_agent.documents.readers import read_pdf
from apex_agent.ingest.imap import SCHEMA
from apex_agent.knowledge import db
from apex_agent.llm.client import LLMClient, NeedsHuman
from apex_agent.settings import load

EVAL = Path(os.environ.get("APEX_EVAL_DIR", "/eval"))
OUT = Path(os.environ.get("APEX_DOCS_DIR", "/app/docs-fde")) / "EXTRACTION_EVAL.md"
INCH_THREAD = {
    "M4x0.7": "#8-32 UNC",
    "M5x0.8": "#10-24 UNC",
    "M6x1.0": "1/4-20 UNC",
    "M8x1.25": "5/16-18 UNC",
    "M10x1.5": "3/8-16 UNC",
}
FIELDS = ["found", "material", "envelope", "holes", "threads", "tolerance", "ra", "general_tolerance", "quantities"]


def close(a: float | None, b: float | None, rel: float = 0.01) -> bool:
    return a is not None and b is not None and abs(a - b) <= max(rel * abs(b), 0.01)


def score_part(item: dict[str, Any], gt: dict[str, Any], part: dict[str, Any] | None) -> dict[str, bool | None]:
    """True/False per field; None = not applicable (e.g. no drawing was sent, so there is no size to read)."""
    if part is None:
        return {"found": False}
    v = {k: f["value"] for k, f in part["fields"].items()}
    drawing = bool(part["files"])
    out: dict[str, bool | None] = {"found": True, "material": v.get("material_code") == item["material"],
                                   "quantities": part["quantities"] == item["quantities"]}  # fmt: skip
    if not drawing:  # a repeat named in the email: only material (from Odoo) and quantities can be checked
        return out
    env = v.get("envelope_mm")
    out["envelope"] = bool(env) and all(close(a, b) for a, b in zip(env or [], gt["envelope_mm"], strict=False))
    if gt["holes"]["count"]:
        out["holes"] = v.get("holes_count") == gt["holes"]["count"] and close(v.get("hole_dia_mm"), gt["holes"]["dia_mm"], 0.02)
    else:
        out["holes"] = not v.get("holes_count")
    want = INCH_THREAD.get(gt["threads"]["size"], "") if gt["units"] == "inch" else gt["threads"]["size"]
    out["threads"] = (v.get("threads_count") or 0, v.get("thread_size") or "") == (
        gt["threads"]["count"],
        want if gt["threads"]["count"] else "",
    )
    tt = gt["tightest_tolerance_mm"]
    got = v.get("tightest_tolerance_mm")
    out["tolerance"] = close(got, tt, 0.05) if tt < 0.1 else (got is None or close(got, tt, 0.05))
    out["ra"] = v.get("ra_um") == gt["ra_um"]
    out["general_tolerance"] = v.get("general_tolerance") == gt["general_tolerance"]
    return out


def summary_size(rows: list[dict[str, Any]]) -> str:
    return f"{sum(1 for r in rows if r['envelope_mm'])} of {len(rows)}"


def _val(req: dict[str, Any], name: str) -> Any:
    return (req.get(name) or {}).get("value")


def source_kind(part: dict[str, Any] | None, gt: dict[str, Any]) -> str:
    if part is None or not part["files"]:
        return "no drawing sent"
    names = " ".join(part["files"]).lower()
    if ".dxf" in names:
        return "DXF"
    return "scan" if gt["scan"] else "vector PDF"


def nist() -> list[dict[str, Any]]:
    """The same path the worker takes: the vector text layer, or OCR + vision when it has no dimension text."""
    llm = LLMClient(load())
    rows = []
    for pdf in sorted((EVAL / "nist").glob("*.pdf")):
        data = pdf.read_bytes()
        f = read_pdf(data, pdf.name)
        path = "vector text"
        if not f:
            ocr = scan.read_ocr(data, pdf.name)
            try:
                f, path = scan.combine(ocr, scan.read_vision(llm, data, pdf.name, uuid.uuid4().hex)), "OCR + vision"
            except NeedsHuman as e:  # e.g. the vision model's daily quota: report what OCR alone found, and why
                f, path = ocr, f"OCR only ({type(e).__name__})"
        v = {k: x["value"] for k, x in f.items()}
        rows.append({"file": pdf.name, "path": path, "units": v.get("units"), "sheets": v.get("sheets"), "gdt": v.get("gdt"),
                     "tightest_tolerance_mm": v.get("tightest_tolerance_mm"), "holes": v.get("holes_count"),
                     "hole_dia_mm": v.get("hole_dia_mm"), "threads": v.get("thread_size"), "ra_um": v.get("ra_um"),
                     "general_tolerance": v.get("general_tolerance"), "envelope_mm": v.get("envelope_mm")})  # fmt: skip
    return rows


def main() -> None:
    labels = [json.loads(x) for x in (EVAL / "rfq_labels.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    seen: set[str] = set()
    conn = db.connect(SCHEMA)
    with conn.cursor() as cur:
        cur.execute("SELECT e.message_id, x.result FROM extractions x JOIN emails e ON e.id = x.email_id")
        results = {mid: res for mid, res in cur.fetchall()}
        cur.execute("""SELECT e.message_id FROM jobs j JOIN emails e ON e.id = j.email_id JOIN gate_results g ON g.email_id = e.id
                       WHERE g.triage = 'rfq' AND j.status = 'queued'""")  # fmt: skip
        waiting = {mid for (mid,) in cur.fetchall()}
    per_field: dict[str, Counter[bool]] = defaultdict(Counter)
    per_kind: dict[str, dict[str, Counter[bool]]] = defaultdict(lambda: defaultdict(Counter))
    req_score: dict[str, Counter[bool]] = defaultdict(Counter)
    misses: list[str] = []
    emails = parts_scored = flagged = 0
    not_yet = len({lab["message_id"] for lab in labels if lab["category"] == "rfq" and lab["message_id"] in waiting})
    for lab in labels:
        if lab["category"] != "rfq" or lab["message_id"] in seen or lab["message_id"] not in results:
            continue
        seen.add(lab["message_id"])
        emails += 1
        res = results[lab["message_id"]]
        by_code = {p["part_code"]: p for p in res["parts"]}
        for item in lab["items"]:
            gt = json.loads((EVAL / "ground_truth" / f"{item['rfq_id']}.json").read_text(encoding="utf-8"))
            part = by_code.get(item["part_code"])
            kind = source_kind(part, gt)
            parts_scored += 1
            flagged += bool(part and any(f.get("needs_human") for f in part["fields"].values()))
            for k, ok in score_part(item, gt, part).items():
                if ok is None:
                    continue
                per_field[k][ok] += 1
                per_kind[kind][k][ok] += 1
                if not ok and len(misses) < 40:
                    misses.append(f"{item['rfq_id']} {item['part_code']} ({kind}, {lab['language']}): **{k}**")
        r, want = res["requirements"], lab["requests"]

        req_score["requested_date"][_val(r, "requested_date") == want["requested_date"]] += 1
        req_score["co2_requested"][bool(_val(r, "co2_requested")) == bool(want["co2"])] += 1
        req_score["export_control"][bool(_val(r, "export_control")) == bool(want["export_control"])] += 1
        req_score["fai"][bool(_val(r, "fai")) == bool(want["fai"])] += 1
        req_score["ppap"][bool(_val(r, "ppap")) == bool(want["ppap"])] += 1
        req_score["incoterms"][bool(_val(r, "incoterms")) == bool(want["incoterms"])] += 1
        req_score["language"][r.get("language") == lab["language"]] += 1
        req_score["route"][(res["route"] == "senior") == bool(want["export_control"])] += 1

    def pct(c: Counter[bool]) -> str:
        n = c[True] + c[False]
        return f"{c[True] / n:.0%} ({c[True]}/{n})" if n else "-"

    summary = {"emails": emails, "parts": parts_scored, "parts_flagged_for_review": flagged, "emails_waiting": not_yet, **{f: round(per_field[f][True] / max(1, sum(per_field[f].values())), 3) for f in FIELDS if per_field[f]},
               "requirements": {k: round(c[True] / max(1, sum(c.values())), 3) for k, c in req_score.items()}}  # fmt: skip
    kinds = sorted(per_kind)
    lines = ["# Extraction evaluation (S4)", "",
             f"_Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `python -m apex_agent.documents.evaluate` from the live "
             "`extractions` table, the traffic labels and the L3 ground truth (read only by this evaluation)._", "",
             f"**{emails} RFQ emails, {parts_scored} parts.** A field is scored only where it can be known: a part named in the email "
             "without a drawing (a repeat) has no size to read, so only its material and quantities count.", "",
             *([f"**{not_yet} labelled RFQ emails are still queued** (not scored here): their scans wait for the vision "
                "model's daily quota (Groq free tier: 200K tokens/day, ≈4,600 per drawing image). The worker retries them "
                "every 30 minutes without using up their attempts.", ""] if not_yet else []),
             f"**{flagged} of {parts_scored} parts** have at least one field flagged for a person to confirm (low confidence, "
             "a conflict between sources, or a size read from a scan).", "",
             "## Per field", "", "| field | all | " + " | ".join(kinds) + " |", "|---|---|" + "---|" * len(kinds)]  # fmt: skip
    for f in FIELDS:
        lines.append(f"| {f} | **{pct(per_field[f])}** | " + " | ".join(pct(per_kind[k][f]) for k in kinds) + " |")
    lines += ["", "Rules: size = length, width, height each within 1 %; holes = count and diameter (2 %); tolerance = the "
              "tightest one when the drawing shows one tighter than 0.1 mm; inch drawings are converted to mm; an inch "
              "thread (\"5/16-18 UNC\") is correct when it is the drawing's thread.", "",
              "## The email (any language)", "", "| requirement | correct |", "|---|---|"]  # fmt: skip
    lines += [f"| {k} | {pct(c)} |" for k, c in req_score.items()]
    lines += ["", "`route` = an export-control marker sent the RFQ to a senior (OPA `apex/extraction/route`).", "",
              "## Misses (first 40)", ""] + ([f"- {m}" for m in misses] or ["- none"])  # fmt: skip
    rows = nist()
    lines += ["", "## NIST MBE PMI test drawings (no ground truth: what the vector-PDF reader finds)", "",
              "Real third-party drawings (inch, ASME Y14.5 GD&T), very unlike Apex's. Test drawings courtesy of NIST.", "",
              "Values are as written when the drawing does not state its units (NIST's notes don't).", "",
              "| file | read by | units | GD&T | tightest tolerance | hole note | thread | Ra | size found |", "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for r in rows:
        lines.append(f"| {r['file']} | {r['path']} | {r['units'] or 'not stated'} | {'yes' if r['gdt'] else '-'} | {r['tightest_tolerance_mm'] or '-'} | "
                     f"{(str(r['holes']) + ' x Ø' + str(r['hole_dia_mm'])) if r['holes'] else '-'} | {r['threads'] or '-'} | {r['ra_um'] or '-'} | "
                     f"{'yes' if r['envelope_mm'] else 'no'} |")  # fmt: skip
    found = sum(1 for r in rows if r["gdt"] or r["tightest_tolerance_mm"])
    summary["nist"] = {
        "drawings": len(rows),
        "with_tolerances_found": found,
        "with_size": sum(1 for r in rows if r["envelope_mm"]),
    }
    lines += ["", f"Tolerances or GD&T found on **{found} of {len(rows)}** NIST drawings, a size on "
              f"{summary_size(rows)}. The readers were built around Apex's drawing conventions; NIST's (PMI drawn as graphics, "
              "no title-block labels, many views, no stated units) show how far that generalises. Anything not read with "
              "confidence goes to a person."]  # fmt: skip
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
