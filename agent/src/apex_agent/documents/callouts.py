"""Read an engineering drawing's TEXT into facts, deterministically (S4). Shared by the DXF, PDF and OCR readers.

Input: text items with their position on the sheet (x right, y UP, sheet units) and whether they run vertically.
- Callouts: "6X Ø3.4 THRU", "4X M6×1.0 - 6H THRU", "2X 5/16-18 UNC - 6H THRU", "2X POCKET", "1X BEND 90°".
- Notes: "DIMENSIONS IN MM|INCHES", "GENERAL TOLERANCES ISO 2768-m" or "±.005 UNLESS NOTED", "Ra 0.4", "FINISH: …".
- Title block: the value that follows each label (CUSTOMER, TITLE, DRAWING NO, REV, MATERIAL, UNITS).
- Tolerances: "24±0.05" on a dimension, a GD&T frame value "Ø.0004", "CRITICAL DIMENSION: … ±0.02".
- Size: the dimension values, told apart by position: the LOWEST horizontal one is the length (under the front
  view), the LEFTMOST vertical one the height, the RIGHTMOST vertical one the width. A hole-pitch dimension sits
  between them and is never taken for a size.
Inch drawings are converted to mm (x 25.4); the thread size is reported as drawn.
"""

import re
from dataclasses import dataclass
from typing import Any

INCH = 25.4
FINISH_KEYWORDS = [("ANODIZ", "OSP-ANODIZE"), ("ZINC", "OSP-PLATING"), ("PLATE", "OSP-PLATING"), ("POWDER", "OSP-POWDER"),
                   ("PASSIVAT", "OSP-PASSIVATE"), ("HARDEN", "OSP-HEATTREAT"), ("HEAT TREAT", "OSP-HEATTREAT")]  # fmt: skip
LABELS = {"CUSTOMER": "customer_on_drawing", "TITLE": "title", "DRAWING NO": "drawing_no", "REV": "rev",
          "MATERIAL": "material_text"}  # fmt: skip
DIA = "[Ø⌀∅Ø]"
NUM = r"(\d*\.?\d+)"
HOLES = re.compile(rf"(\d+)\s*X\s*{DIA}\s*{NUM}(?!\s*-\s*6H)", re.I)  # "6X Ø3.4 THRU", "4X Ø.250" (a hole note)
THREADS = re.compile(r"(\d+)\s*X\s*(M\d+(?:\s*[×xX]\s*[\d.]+)?|#?\d+(?:/\d+)?-\d+\s*UN[CF])\s*-\s*6H", re.I)
POCKETS = re.compile(r"(\d+)\s*X\s*POCKET", re.I)
BENDS = re.compile(r"(\d+)\s*X\s*BEND", re.I)
RA = re.compile(rf"\bRa\s*{NUM}")
PLUS_MINUS = re.compile(rf"±\s*{NUM}")
BILATERAL = re.compile(rf"^\s*[+-]\s*{NUM}\s*$")  # "+.003" / "-.000": one limit of a bilateral tolerance
GDT_VALUE = re.compile(rf"^\s*{DIA}\s*{NUM}\s*$")  # a lone "Ø.0004" in a feature control frame
PLAIN_NUMBER = re.compile(rf"^\s*{DIA}?\s*{NUM}\s*$")


@dataclass
class TextItem:
    text: str
    x: float = 0.0
    y: float = 0.0  # up
    vertical: bool = False
    page: int = 0


def _f(value: Any, source: str, confidence: float, note: str = "") -> dict[str, Any]:
    return {"value": value, "source": source, "confidence": confidence, "note": note}


def _num(item: TextItem) -> float:
    m = PLAIN_NUMBER.match(item.text.split("±")[0])
    return float(m[1]) if m else 0.0


def units_of(items: list[TextItem]) -> str | None:
    for it in items:
        t = it.text.upper()
        if "DIMENSIONS IN INCH" in t or t.strip() == "INCH":
            return "inch"
        if "DIMENSIONS IN MM" in t:
            return "mm"
    return None


def parse(items: list[TextItem], source: str, confidence: float, dims: list[TextItem] | None = None) -> dict[str, dict[str, Any]]:
    """Facts from text items. `dims`: dimension texts with positions (DXF gives them exactly; else they are found
    among `items` as lone numbers)."""
    out: dict[str, dict[str, Any]] = {}
    units = units_of(items)
    k = INCH if units == "inch" else 1.0
    if units:
        out["units"] = _f(units, source, confidence)
    texts = [it.text.strip() for it in items]
    joined = "\n".join(texts)

    # title block: "LABEL: value" on one line (vision/OCR), or the text right after a label (CAD/PDF)
    for t in list(texts):
        if (m := re.match(r"^(CUSTOMER|TITLE|DRAWING NO|REV|MATERIAL)\s*:\s*(.+)$", t, re.I)) and LABELS[m[1].upper()] not in out:
            texts.append(m[1].upper())
            texts.append(m[2].strip())
    for i, t in enumerate(texts[:-1]):
        field = LABELS.get(t.upper())
        if field and field not in out:
            val = texts[i + 1]
            if field == "material_text" and re.search(r"PURCHASE ORDER|SEE P\.?O|PER ORDER", val, re.I):
                out[field] = _f(None, source, confidence, "drawing says: material per purchase order")
            else:
                out[field] = _f(val, source, confidence)
    if "drawing_no" not in out and (m := re.search(r"\bD-\d{5}\b", joined)):
        out["drawing_no"] = _f(m.group(0), source, confidence * 0.8, "pattern, not the title block")

    if m := HOLES.search(joined):
        out["holes_count"] = _f(int(m[1]), source, confidence)
        out["hole_dia_mm"] = _f(round(float(m[2]) * k, 2), source, confidence)
    else:
        # A turned part's bore is drawn as a diameter dimension, not a callout: the diameters smaller than the
        # outside diameter (the largest one) are bores. GD&T values (< 0.5 mm) are not diameters.
        dias = sorted({float(m[1]) * k for it in items if (m := re.match(rf"^\s*{DIA}\s*{NUM}", it.text.split("±")[0]))
                       and float(m[1]) * k >= 0.5}, reverse=True)  # fmt: skip
        if len(dias) >= 2:
            out["holes_count"] = _f(len(dias) - 1, source, confidence * 0.85, "bore(s) from diameter dimensions")
            out["hole_dia_mm"] = _f(round(dias[-1], 2), source, confidence * 0.85, "bore from a diameter dimension")
        else:
            out["holes_count"] = _f(0, source, confidence * 0.9, "no hole callout")
    if m := THREADS.search(joined):
        out["threads_count"] = _f(int(m[1]), source, confidence)
        out["thread_size"] = _f(re.sub(r"\s*[×X]\s*", "x", m[2].strip()), source, confidence)
    else:
        out["threads_count"] = _f(0, source, confidence * 0.9, "no thread callout")
    out["pockets"] = _f(int(m[1]) if (m := POCKETS.search(joined)) else 0, source, confidence * (1 if m else 0.9))
    out["bends"] = _f(int(m[1]) if (m := BENDS.search(joined)) else 0, source, confidence * (1 if m else 0.9))
    if m := RA.search(joined):
        out["ra_um"] = _f(float(m[1]), source, confidence)  # Ra is always in µm
    if m := re.search(r"ISO\s*2768\s*-\s*([fmcv])\b", joined, re.I):
        out["general_tolerance"] = _f(f"ISO 2768-{m[1].lower()}", source, confidence)
    elif m := re.search(rf"GENERAL TOLERANCES\s*±\s*{NUM}", joined, re.I):
        out["general_tolerance"] = _f(f"±{float(m[1]):g} in" if units == "inch" else f"±{float(m[1]):g} mm", source, confidence)
    fin = next((t for t in texts if re.match(r"^\d+\.\s*FINISH:", t, re.I)), None) or next(
        (texts[i + 1] for i, t in enumerate(texts[:-1]) if t.upper() == "FINISH"), None)  # fmt: skip
    if fin is not None:
        osp = sorted({code for kw, code in FINISH_KEYWORDS if kw in fin.upper()})
        out["outside_processes"] = _f(osp, source, confidence, fin)

    # tolerances: explicit ± (not the general note), GD&T frame values; the tightest one wins
    tols, gdt = [], False
    for t in list(texts):
        if "UNLESS NOTED" in t.upper() or "GENERAL TOLERANCE" in t.upper():
            continue
        tols += [float(v) * k for v in PLUS_MINUS.findall(t)]
        if (m := BILATERAL.match(t)) and float(m[1]) > 0:
            tols.append(float(m[1]) * k)
        if (m := GDT_VALUE.match(t)) and float(m[1]) * k < 0.5:
            tols.append(float(m[1]) * k)
            gdt = True
    out["gdt"] = _f(gdt, source, confidence)
    out["tightest_tolerance_mm"] = (_f(round(min(tols), 4), source, confidence) if tols
                                    else _f(None, source, confidence * 0.9, "no tolerance tighter than the general note"))  # fmt: skip

    # overall size from the dimension texts
    cand = dims if dims is not None else [it for it in items if PLAIN_NUMBER.match(it.text.split("±")[0]) and it.page == 0]
    horiz = [d for d in cand if not d.vertical]
    vert = [d for d in cand if d.vertical]
    if horiz and len(vert) >= 2:
        length = _num(min(horiz, key=lambda d: d.y)) * k
        height = _num(min(vert, key=lambda d: d.x)) * k
        width = _num(max(vert, key=lambda d: d.x)) * k
        out["envelope_mm"] = _f([round(length, 2), round(width, 2), round(height, 2)], source, confidence * 0.95,
                                "L from the lowest horizontal dim, H leftmost / W rightmost vertical dims")  # fmt: skip
    return out
