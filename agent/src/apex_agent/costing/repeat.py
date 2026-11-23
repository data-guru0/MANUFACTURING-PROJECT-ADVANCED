"""Repeat parts and revisions (S12, W10: re-quoting from scratch). Deterministic, no LLM: graph/draft.py words a revision's diff.

Before extraction, every drawing in the RFQ is checked against Apex's archive, IN ORDER, and only ever against parts of
the SAME customer (a match never crosses customers: their drawings and prices are theirs):

    1 file hash            the same bytes as an archived drawing (part_specs.files, from Odoo's attachments)
    2 customer + part no.  the same drawing number: same rev = repeat, a later rev = revision of the latest earlier one
    3 geometric fingerprint  same title, sorted envelope, holes, tolerance and material (a renumbered drawing)
    4 embedding similarity a HINT only ("looks like D-10046-B"), never reused: similar is not identical

    repeat    the approved spec is reused and repriced at today's costs; old and new price are shown with the reasons
    revision  diffed (DXF entities; PDF text + a rendered pixel diff); the LLM summarises the diff; the cost of the
              change is the engine's estimate of the new spec minus the old spec, both at today's costs

Usage (knowledge job): python -m apex_agent.costing.repeat eval   # the 80 labelled RFQs, and each again as another customer
"""

import asyncio
import base64
import difflib
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf

from apex_agent.documents import readers, scan
from apex_agent.documents.extract import _part_key

EMBED_MIN = 0.93  # cosine similarity for a "looks like" hint (bge-small); measured in the eval, not tuned to it
FIELDS = ("envelope_mm", "holes_count", "hole_dia_mm", "threads_count", "thread_size", "pockets", "bends",
          "tightest_tolerance_mm", "ra_um", "material_text", "outside_processes", "general_tolerance")  # fmt: skip
ORDER = {"dxf": 0, "pdf": 1, "step": 2, "stp": 2}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_drawings(files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    """One group per drawing (file stem): its files, their hashes and what the exact readers / local OCR say (no LLM)."""
    groups: dict[str, dict[str, Any]] = {}
    for name, data in sorted(files, key=lambda f: ORDER.get(f[0].lower().rsplit(".", 1)[-1], 9)):
        ext = name.lower().rsplit(".", 1)[-1]
        stem = re.sub(r"(_scan)?\.[^.]+$", "", name, flags=re.I).lower()
        g = groups.setdefault(stem, {"files": {}, "sha": [], "facts": {}})
        g["files"][ext] = (name, data)
        g["sha"].append(sha256(data))
        facts: dict[str, Any] = {}
        if ext == "dxf":
            facts = readers.read_dxf(data, name)
        elif ext == "pdf":
            facts = readers.read_pdf(data, name) or scan.read_ocr(data, name)  # a scan: local OCR only
        for k, f in facts.items():
            if f.get("value") is not None:
                g["facts"].setdefault(k, f["value"])  # DXF first: the most exact source wins
    return [g for g in groups.values() if g["facts"].get("drawing_no") or g["sha"]]


def fingerprint(f: dict[str, Any]) -> tuple[Any, ...] | None:
    env = f.get("envelope_mm")
    if not env or not f.get("title"):
        return None
    return (str(f["title"]).upper(), tuple(sorted(round(float(x)) for x in env)), f.get("holes_count"),
            f.get("tightest_tolerance_mm"), str(f.get("material_text") or "").upper())  # fmt: skip


def approved_spec(conn: Any, code: str) -> tuple[str | None, dict[str, Any]]:
    """(family, field values) of an archived part: what Apex's own drawing says, with its BOM material code."""
    with conn.cursor() as cur:
        cur.execute("SELECT family, fields, material FROM part_specs WHERE code = %s", (code,))
        row = cur.fetchone()
    if not row:
        return None, {}
    return row[0], {**{k: v.get("value") for k, v in row[1].items()}, "material_code": row[2]}


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def match(conn: Any, customer: str | None, g: dict[str, Any], *, embed: bool = True) -> dict[str, Any]:
    f = g["facts"]
    dno, rev = f.get("drawing_no"), str(f.get("rev") or "A").upper()
    if not customer:
        return {"kind": "new", "why": "sender is not a known customer: no archive match (a match never crosses customers)"}
    with conn.cursor() as cur:
        cur.execute("""SELECT s.code, p.customer_ref FROM part_specs s JOIN parts p USING (code)
                       WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(s.files) x WHERE x->>'sha256' = ANY(%s))
                       ORDER BY s.code""", (g["sha"],))  # fmt: skip
        hits = cur.fetchall()
        same = [c for c, cu in hits if cu == customer]
        if same:
            return {"kind": "repeat", "method": "file hash", "of": same[0],
                    "why": f"the identical file is in Apex's archive as {same[0]}"}  # fmt: skip
        blocked = len([c for c, cu in hits if cu != customer])
        if dno:
            cur.execute("SELECT code FROM parts WHERE drawing = %s AND customer_ref = %s ORDER BY code", (dno, customer))
            codes = [r[0] for r in cur.fetchall()]
            if f"{dno}-{rev}" in codes:
                return {"kind": "repeat", "method": "customer + part number", "of": f"{dno}-{rev}",
                        "why": f"this customer's {dno} rev {rev} was quoted before"}  # fmt: skip
            earlier = [c for c in codes if c.rsplit("-", 1)[-1] < rev]
            if earlier:
                # diff against the CLOSEST earlier rev (fewest changed fields; a tie -> the latest): a rev C may be
                # drawn from A, not from B
                cur.execute("SELECT code, fields FROM part_specs WHERE code = ANY(%s)", (earlier,))
                specs = {c: {k: v.get("value") for k, v in fl.items()} for c, fl in cur.fetchall()}
                best = min(reversed(earlier), key=lambda c: len(field_changes(specs.get(c, {}), f)) if c in specs else 99)
                return {"kind": "revision", "method": "customer + part number", "of": best,
                        "why": f"rev {rev} of {dno}; closest earlier rev quoted by Apex: {best.rsplit('-', 1)[-1]}"
                               + (f" (of {', '.join(c.rsplit('-', 1)[-1] for c in earlier)})" if len(earlier) > 1 else "")}  # fmt: skip
        fp = fingerprint(f)
        cur.execute("SELECT s.code, s.fields FROM part_specs s JOIN parts p USING (code) WHERE p.customer_ref = %s ORDER BY s.code",
                    (customer,))  # fmt: skip
        for code, fields in cur.fetchall():
            if fp and fingerprint({k: v.get("value") for k, v in fields.items()}) == fp:
                return {"kind": "repeat", "method": "geometric fingerprint", "of": code,
                        "why": f"same shape, size, holes, tolerance and material as {code} (a renumbered drawing)"}  # fmt: skip
        out: dict[str, Any] = {"kind": "new"}
        if blocked:
            out["why"] = "an identical file is archived for ANOTHER customer: not matched (never across customers)"
        if embed and fp:
            from apex_agent.knowledge.embed import embed_query

            text = f"{f.get('title')} {f.get('material_text') or ''} {' x '.join(str(x) for x in f['envelope_mm'])} mm"
            cur.execute("""SELECT code, 1 - (embedding <=> %s::vector) FROM parts WHERE customer_ref = %s AND embedding IS NOT NULL
                           ORDER BY embedding <=> %s::vector LIMIT 1""", (_vec(embed_query(text)), customer, _vec(embed_query(text))))  # fmt: skip
            row = cur.fetchone()
            if row and float(row[1]) >= EMBED_MIN:
                out["similar"] = {"code": row[0], "similarity": round(float(row[1]), 3),
                                  "note": "looks like this customer's part (embedding): a hint for the estimator, not reused"}  # fmt: skip
        return out


def detect(conn: Any, customer: str | None, files: list[tuple[str, bytes]], *, embed: bool = True,
           parts: Sequence[dict[str, Any]] = ()) -> dict[str, dict[str, Any]]:  # fmt: skip
    """{part key (D-xxxxx-REV, as extraction names it): match}. Matches of a revision keep `_group` for the diff.
    `parts` (the extraction's) name each drawing's rev: they read the email too, while local OCR of a noisy scan can
    misread a rev letter (C as A: a revision then looked like a repeat). A part asked for by number with no drawing
    attached ("re-quote D-10042-A") is matched by customer + part number alone."""
    named = {str(p["part_code"]).rsplit("-", 1)[0]: p for p in parts if "-" in str(p.get("part_code") or "")}
    out: dict[str, dict[str, Any]] = {}
    for g in read_drawings(files):
        f = g["facts"]
        if not f.get("drawing_no"):
            continue
        if p := named.get(f["drawing_no"]):  # the extraction's consolidated reading wins (it is what gets costed)
            f["rev"] = str(p["part_code"]).rsplit("-", 1)[-1]
            f.update({k: v["value"] for k, v in (p.get("fields") or {}).items() if v.get("value") is not None})
        m = match(conn, customer, g, embed=embed)
        m["_group"] = g
        out[_part_key(f"{f['drawing_no']}-{f.get('rev') or 'A'}")] = m
    for p in parts:
        code = str(p.get("part_code") or "")
        if "-" in code and code not in out and not p.get("files"):
            dno, rev = code.rsplit("-", 1)
            m = match(conn, customer, {"facts": {"drawing_no": dno, "rev": rev}, "sha": [], "files": {}}, embed=False)
            out[code] = {
                **m,
                "_group": {"files": {}, "facts": {}},
                "why": m.get("why", "") + " (by part number: no drawing attached)",
            }
    return out


# ---- revisions: what changed
def dxf_entities(data: bytes) -> Counter[tuple[str, Any]]:
    from ezdxf import recover

    doc, _ = recover.read(io.BytesIO(data))
    sig: Counter[tuple[str, Any]] = Counter()
    for e in doc.modelspace():
        t = e.dxftype()
        key: Any = ()
        if t == "LINE":
            key = tuple(round(v, 1) for p in (e.dxf.start, e.dxf.end) for v in (p.x, p.y))
        elif t in ("CIRCLE", "ARC"):
            key = (round(e.dxf.center.x, 1), round(e.dxf.center.y, 1), round(e.dxf.radius, 2))
        elif t == "TEXT":
            key = e.dxf.text
        elif t == "MTEXT":
            key = e.plain_text(split=False)
        elif t == "DIMENSION":
            try:
                key = round(float(e.get_measurement()), 3)
            except (TypeError, ValueError):
                key = ()
        sig[(t, key)] += 1
    return sig


def dxf_diff(old: bytes, new: bytes) -> dict[str, Any]:
    a, b = dxf_entities(old), dxf_entities(new)
    added, removed = b - a, a - b

    def per_type(c: Counter[tuple[str, Any]]) -> dict[str, int]:
        n: Counter[str] = Counter()
        for (t, _), k in c.items():
            n[t] += k
        return dict(n)

    def texts(c: Counter[tuple[str, Any]]) -> list[str]:
        return [str(k) for (t, k) in c if t in ("TEXT", "MTEXT")][:12]

    return {"added": per_type(added), "removed": per_type(removed), "text_added": texts(added), "text_removed": texts(removed)}


def _gray(page: Any, dpi: int) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]


def pdf_diff(old: bytes, new: bytes, dpi: int = 50) -> dict[str, Any]:
    """Text lines added/removed, and the share of the first page's pixels that changed, with a highlight image."""
    with pymupdf.open(stream=old, filetype="pdf") as a, pymupdf.open(stream=new, filetype="pdf") as b:
        ta = [ln for p in a for ln in p.get_text().splitlines() if ln.strip()]
        tb = [ln for p in b for ln in p.get_text().splitlines() if ln.strip()]
        out: dict[str, Any] = {"text_added": [], "text_removed": []}
        for ln in difflib.ndiff(ta, tb):
            if ln[:2] in ("+ ", "- "):
                out["text_added" if ln[0] == "+" else "text_removed"].append(ln[2:])
        out = {k: v[:12] for k, v in out.items()}
        if not tb:  # a scan: no text layer and noisy pixels; the DXF and the fields carry the diff
            return {"pixels": {"skipped": "the new PDF is a scan: a text or pixel diff against a clean plot is noise"},
                    "text_added": [], "text_removed": []}  # fmt: skip
        ga, gb = _gray(a[0], dpi), _gray(b[0], dpi)
    # ponytail: page 1, 50 dpi, no alignment: a re-plotted sheet with a shifted frame lights up everywhere;
    # align on the title block (phase correlation) first if customers re-plot old drawings.
    h, w = min(ga.shape[0], gb.shape[0]), min(ga.shape[1], gb.shape[1])
    changed = np.abs(ga[:h, :w].astype(np.int16) - gb[:h, :w].astype(np.int16)) > 48
    rgb = np.repeat((gb[:h, :w] // 2 + 127)[:, :, None], 3, axis=2)  # the new drawing, faded
    rgb[changed] = (220, 0, 0)  # what changed, in red
    png = pymupdf.Pixmap(pymupdf.csRGB, w, h, np.ascontiguousarray(rgb, dtype=np.uint8).tobytes(), 0).tobytes("png")
    ys, xs = np.nonzero(changed)
    out["pixels"] = {"changed_pct": round(float(changed.mean()) * 100, 2), "dpi": dpi,
                     "bbox_px": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())] if len(xs) else None,
                     "highlight_png_b64": base64.b64encode(png).decode()}  # fmt: skip
    return out


def field_changes(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"field": k, "old": old.get(k), "new": new.get(k)} for k in FIELDS
            if old.get(k) is not None and new.get(k) is not None and old.get(k) != new.get(k)]  # fmt: skip


def diff(old_files: dict[str, tuple[str, bytes]], old_fields: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"fields": field_changes(old_fields, g["facts"])}
    if "dxf" in old_files and "dxf" in g["files"]:
        out["dxf"] = dxf_diff(old_files["dxf"][1], g["files"]["dxf"][1])
    if "pdf" in old_files and "pdf" in g["files"]:
        out["pdf"] = pdf_diff(old_files["pdf"][1], g["files"]["pdf"][1])
    return out


def fetch_archive(code: str) -> dict[str, tuple[str, bytes]]:
    """Apex's archived drawing files of a part, through erp-mcp-read (OPA: node `repeat` may call erp.get_drawing)."""
    from apex_agent.tools import call_tool

    ok, r = asyncio.run(call_tool("erp-mcp-read", "get_drawing", {"code": code}, "repeat"))
    if not ok or not isinstance(r, dict) or not r.get("found"):
        return {}
    return {f["name"].lower().rsplit(".", 1)[-1]: (f["name"], base64.b64decode(f["data_b64"])) for f in r["files"]}


def public(m: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if not k.startswith("_")}


# ---- evaluation (no LLM)
def evaluate() -> dict[str, Any]:
    from apex_agent.knowledge import db

    ev = Path(os.environ.get("APEX_EVAL_DIR", "/eval"))
    labels = json.loads((ev / "rfq_parts.json").read_text(encoding="utf-8"))
    customers = sorted({lab["customer_key"] for lab in labels})
    res: dict[str, Any] = {"repeat": [0, 0], "revision": [0, 0], "wrong": [], "false_matches": [], "cross_customer_matches": [],
                           "methods": Counter(), "hints": 0, "diffs": []}  # fmt: skip
    conn = db.connect()
    for lab in labels:
        files = [(p.name, p.read_bytes()) for p in sorted((ev / "rfq_drawings").glob(f"{lab['rfq_id']}.*"))]
        got = detect(conn, lab["customer_key"], files)
        m = got.get(_part_key(lab["part_code"])) or {"kind": "new"}
        res["hints"] += "similar" in m
        if lab["kind"] in ("repeat", "revision"):
            res[lab["kind"]][1] += 1
            if m["kind"] == lab["kind"] and m.get("of") == lab["source_part"]:
                res[lab["kind"]][0] += 1
                res["methods"][f"{lab['kind']}: {m['method']}"] += 1
                if lab["kind"] == "revision" and len(res["diffs"]) < 12:
                    with conn.cursor() as cur:
                        cur.execute("SELECT fields FROM part_specs WHERE code = %s", (m["of"],))
                        old = {k: v.get("value") for k, v in (cur.fetchone() or [{}])[0].items()}
                    d = diff(fetch_archive(m["of"]), old, m["_group"])
                    res["diffs"].append({"rfq": lab["rfq_id"], "old": m["of"], "new": lab["part_code"], "fields": d["fields"],
                                         "dxf": (d.get("dxf") or {}).get("added"),
                                         "pixels_pct": ((d.get("pdf") or {}).get("pixels") or {}).get("changed_pct")})  # fmt: skip
            else:
                res["wrong"].append({"rfq": lab["rfq_id"], "label": lab["kind"], "of": lab["source_part"], "got": public(m)})
        elif m["kind"] != "new":
            res["false_matches"].append({"rfq": lab["rfq_id"], "label": lab["kind"], "got": public(m)})
        other = customers[(customers.index(lab["customer_key"]) + 1) % len(customers)]  # the same files from another customer
        for k, x in detect(conn, other, files, embed=False).items():
            if x["kind"] != "new":
                res["cross_customer_matches"].append({"rfq": lab["rfq_id"], "as": other, "part": k, "got": public(x)})
    res["methods"] = dict(res["methods"])
    return res


if __name__ == "__main__":
    if sys.argv[1:] != ["eval"]:
        raise SystemExit(__doc__)
    r = evaluate()
    out = Path(os.environ.get("APEX_DOCS_DIR", "/app/docs-fde")) / "REPEAT_EVAL.md"
    lines = ["# Repeat parts and revisions (S12)", "",
             "_Generated by `python -m apex_agent.costing.repeat eval` from the 80 labelled RFQs (L3) against Apex's archive "
             "(Odoo attachments, read into `part_specs`). No LLM: the revision summary is not part of the evaluation._", "",
             f"- Repeats detected (right part): **{r['repeat'][0]} of {r['repeat'][1]}**",
             f"- Revisions detected (right previous rev): **{r['revision'][0]} of {r['revision'][1]}**",
             f"- False matches on new / out-of-capability RFQs: **{len(r['false_matches'])}**",
             f"- Matches when the same files come from ANOTHER customer: **{len(r['cross_customer_matches'])}** (must be 0)",
             f"- 'Looks like' embedding hints (never reused): {r['hints']}", "",
             "| How it matched | RFQs |", "|---|---|", *[f"| {k} | {v} |" for k, v in sorted(r["methods"].items())], "",
             "## What changed in each revision (computed: fields, DXF entities, PDF pixels)", "",
             "| RFQ | Old | New | Field changes | DXF entities added | PDF pixels changed |", "|---|---|---|---|---|---|",
             *[f"| {d['rfq']} | {d['old']} | {d['new']} | {'; '.join(f'{c['field']}: {c['old']} -> {c['new']}' for c in d['fields']) or '-'} "
               f"| {d['dxf'] or '-'} | {d['pixels_pct'] if d['pixels_pct'] is not None else 'scan'} % |" for d in r["diffs"]], ""]  # fmt: skip
    out.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({k: v for k, v in r.items() if k != "diffs"}, ensure_ascii=False, default=str))
