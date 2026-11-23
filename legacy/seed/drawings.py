"""L3: engineering drawings for every part: DXF (ezdxf), vector PDF, and STEP (CadQuery) for ~30 %.

Each drawing is an A3 landscape sheet (420 x 297 mm, 1 drawing unit = 1 mm of paper) with:
three views at a standard scale, real DIMENSION entities (true values via dimlfac), a tolerance on the
critical dimension, hole and thread callouts, a GD&T position frame when the tolerance is <= 0.02 mm,
a surface-finish symbol, general notes (ISO 2768-m) and a title block.

The drawing is generated from the SAME parameters the L2 price was built on, and a ground-truth JSON is
written next to it (data/generated/ground_truth/), for evaluation only: never given to the agent.

PDF: ezdxf renders the geometry and the text as vector outlines; PyMuPDF then adds an invisible text
layer at the same positions, like many CAD-exported PDFs, so the text is searchable.

ponytail: views are simplified outlines (rectangles, L-shapes, circles), not true 3D projections;
the dimensions, tolerances, callouts and title block are real. Upgrade: project the CadQuery solid.
"""

import json
import math
import random
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import ezdxf
import pymupdf
from ezdxf.addons.drawing import Frontend, RenderContext, config, layout
from ezdxf.addons.drawing import pymupdf as pdf_backend
from ezdxf.document import Drawing
from ezdxf.layouts import Modelspace
from ezdxf.math import BoundingBox2d

from legacy.seed.history import PartRec

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "generated"
SHEET_W, SHEET_H = 420.0, 297.0
SCALES = [2.0, 1.0, 0.5, 0.2, 0.1, 0.05]
DRILL = [3.4, 4.5, 5.5, 6.6, 9.0, 11.0, 13.5]
THREADS = [("M4x0.7", 4), ("M5x0.8", 5), ("M6x1.0", 6), ("M8x1.25", 8), ("M10x1.5", 10)]
INCH_THREAD = {"M4x0.7": "#8-32 UNC", "M5x0.8": "#10-24 UNC", "M6x1.0": "1/4-20 UNC", "M8x1.25": "5/16-18 UNC",
               "M10x1.5": "3/8-16 UNC"}  # fmt: skip
MATERIAL_NAMES = {
    "MAT-AL6061": "ALUMINIUM 6061-T6", "MAT-AL7075": "ALUMINIUM 7075-T6", "MAT-SS304": "STAINLESS STEEL 304",
    "MAT-SS316": "STAINLESS STEEL 316", "MAT-1018": "STEEL AISI 1018", "MAT-S235": "STEEL S235JR",
    "MAT-C360": "BRASS C360", "MAT-POM": "POM (ACETAL)", "MAT-TI64": "TITANIUM Ti-6Al-4V",
}  # fmt: skip
FINISH_NAMES = {"OSP-ANODIZE": "ANODIZE TYPE II CLEAR", "OSP-PLATING": "ZINC PLATE CLEAR TRIVALENT",
                "OSP-POWDER": "POWDER COAT RAL 7035", "OSP-PASSIVATE": "PASSIVATE ASTM A967",
                "OSP-HEATTREAT": "HARDEN & TEMPER 40-45 HRC"}  # fmt: skip


@dataclass
class Variant:
    """How this particular drawing differs from a clean in-house drawing (RFQ variety)."""

    units: str = "mm"  # "mm" | "inch"
    material_in_email: bool = False  # title block says SEE PURCHASE ORDER
    sheets: int = 1
    scan: bool = False
    step: bool = False


@dataclass
class Features:
    """What the drawing shows beyond the costing spec, chosen deterministically from the part code."""

    hole_dia: float
    hole_xy: list[tuple[float, float]]  # in part mm, on the top/end view
    thread: str
    thread_xy: list[tuple[float, float]]
    critical: str  # which dimension carries the tight tolerance
    gdt: bool


@dataclass
class GroundTruth:
    drawing_id: str
    part_code: str
    drawing_no: str
    rev: str
    family: str
    customer_key: str
    material: str
    material_text_on_drawing: str | None
    material_in_email: bool
    units: str
    envelope_mm: list[float]
    holes: dict[str, Any]
    threads: dict[str, Any]
    pockets: int
    bends: int
    tightest_tolerance_mm: float
    general_tolerance: str
    ra_um: float
    gdt: bool
    outside_processes: list[str]
    sheets: int
    scan: bool
    kind: str
    source_part: str | None
    files: dict[str, str] = field(default_factory=dict)
    step_bbox_mm: list[float] | None = None
    step_volume_cm3: float | None = None


# ------------------------------------------------------------------ features
def features(p: PartRec) -> Features:
    rng = random.Random(p.code.rsplit("-", 1)[0])  # same features for Rev A and Rev B
    s = p.spec
    if s.process == "turn":
        bore = p.params.get("bore", 0.0)
        hole_dia = float(bore) if bore else rng.choice(DRILL[:4])
        holes = [(0.0, 0.0)] if bore else _bolt_circle(s.holes, s.width * 0.35)
        size = min(t for t in THREADS if t[1] <= max(4, s.width / 4))[0] if s.threads else ""
        threads = _bolt_circle(s.threads, s.width * 0.3, phase=0.4) if s.threads else []
    else:
        hole_dia = rng.choice([d for d in DRILL if d < min(s.length, s.width) / 4] or [DRILL[0]])
        holes = _grid(s.holes, s.length, s.width, rng)
        fits = [t for t in THREADS if t[1] < min(s.length, s.width) / 5] or [THREADS[0]]
        size = rng.choice(fits)[0] if s.threads else ""
        threads = _grid(s.threads, s.length * 0.6, s.width * 0.5, rng, offset=(s.length * 0.2, s.width * 0.25))
    critical = "hole pitch" if len(holes) >= 2 else ("bore" if s.process == "turn" else "overall length")
    return Features(hole_dia, holes, size, threads, critical, s.tolerance_mm <= 0.02)


def _grid(n: int, L: float, W: float, rng: random.Random, offset: tuple[float, float] = (0.0, 0.0)) -> list[tuple[float, float]]:
    if n <= 0:
        return []
    cols = max(1, math.ceil(math.sqrt(n * L / max(W, 1))))
    rows = math.ceil(n / cols)
    mx, my = L * 0.15, W * 0.2
    pts = []
    for i in range(n):
        c, r = i % cols, i // cols
        x = mx + (L - 2 * mx) * (c / (cols - 1) if cols > 1 else 0.5)
        y = my + (W - 2 * my) * (r / (rows - 1) if rows > 1 else 0.5)
        pts.append((round(offset[0] + x, 1), round(offset[1] + y, 1)))
    return pts


def _bolt_circle(n: int, r: float, phase: float = 0.0) -> list[tuple[float, float]]:
    return [(round(r * math.cos(phase + 2 * math.pi * i / n), 2), round(r * math.sin(phase + 2 * math.pi * i / n), 2))
            for i in range(n)]  # fmt: skip


# ------------------------------------------------------------------ DXF
def fmt_len(mm: float, units: str) -> str:
    return f"{mm / 25.4:.3f}".lstrip("0") if units == "inch" else f"{mm:g}"


def tol_text(tol: float, units: str) -> str:
    return f"±{tol / 25.4:.4f}".replace("0.", ".") if units == "inch" else f"±{tol:g}"


def pick_scale(L: float, W: float, H: float) -> float:
    for s in SCALES:
        # front + right view side by side, top view above front; callout band top-right (x >= 235, y >= 240)
        if (L + W) * s <= 290 and L * s <= 190 and (H + W) * s <= 140:
            return s
    return SCALES[-1]


def scale_label(s: float) -> str:
    return f"{int(s)}:1" if s >= 1 else f"1:{round(1 / s)}"


def build_dxf(p: PartRec, f: Features, v: Variant, drawing_id: str, customer: str, issued: date) -> Drawing:
    s = p.spec
    doc = ezdxf.new("R2018", setup=True, units=4)  # 4 = millimetres
    doc.styles.add("ISO", font="DejaVuSans.ttf")
    msp = doc.modelspace()
    for name, color, lt in [("OUTLINE", 7, "CONTINUOUS"), ("CENTER", 1, "CENTER"), ("DIM", 3, "CONTINUOUS"),
                            ("TEXT", 7, "CONTINUOUS"), ("FRAME", 7, "CONTINUOUS"), ("HIDDEN", 8, "DASHED")]:  # fmt: skip
        doc.layers.add(name, color=color, linetype=lt)
    unit_factor = 1 / 25.4 if v.units == "inch" else 1.0
    L, W, H = s.length, s.width, s.height
    sc = pick_scale(L, W, H)
    lfac = unit_factor / sc
    dec = 3 if v.units == "inch" else (2 if s.tolerance_mm < 0.1 else 1)
    dimstyle = {"dimlfac": lfac, "dimdec": dec, "dimtxt": 3.0, "dimasz": 2.5, "dimclrd": 3, "dimclre": 3,
                "dimtzin": 8 if v.units == "mm" else 4, "dimtxsty": "ISO", "dimtdec": 4 if v.units == "inch" else 3}  # fmt: skip

    fx, fy = 40.0, 88.0  # front view origin (bottom-left)
    tx, ty = fx, fy + H * sc + 35  # top view above the front view
    rx, ry = fx + L * sc + 45, fy  # right/end view to the right
    turned = s.process == "turn"

    def rect(x: float, y: float, w: float, h: float, layer: str = "OUTLINE") -> None:
        msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True, dxfattribs={"layer": layer})

    def dim(p1: tuple[float, float], p2: tuple[float, float], base: tuple[float, float], angle: float = 0,
            tol: float | None = None) -> None:  # fmt: skip
        d = msp.add_linear_dim(base=base, p1=p1, p2=p2, angle=angle, dimstyle="EZDXF", override=dict(dimstyle),
                               dxfattribs={"layer": "DIM"})  # fmt: skip
        if tol is not None:
            d.set_tolerance(tol * unit_factor, tol * unit_factor, hfactor=0.8)
        d.render()

    # --- front view
    if p.family == "l_bracket":
        t = p.params.get("t", H / 5) * sc
        msp.add_lwpolyline([(fx, fy), (fx + L * sc, fy), (fx + L * sc, fy + t), (fx + t, fy + t), (fx + t, fy + H * sc),
                            (fx, fy + H * sc)], close=True, dxfattribs={"layer": "OUTLINE"})  # fmt: skip
    elif p.family == "u_bracket":
        t = p.params.get("t", H / 5) * sc
        msp.add_lwpolyline([(fx, fy), (fx + L * sc, fy), (fx + L * sc, fy + H * sc), (fx + L * sc - t, fy + H * sc),
                            (fx + L * sc - t, fy + t), (fx + t, fy + t), (fx + t, fy + H * sc), (fx, fy + H * sc)],
                           close=True, dxfattribs={"layer": "OUTLINE"})  # fmt: skip
    else:
        rect(fx, fy, L * sc, H * sc)
    if turned:
        msp.add_line((fx - 5, fy + H * sc / 2), (fx + L * sc + 5, fy + H * sc / 2), dxfattribs={"layer": "CENTER"})
        if s.pockets and p.family == "keyed_shaft":
            rect(fx + L * sc * 0.25, fy + H * sc * 0.8, L * sc * 0.4, H * sc * 0.2, layer="HIDDEN")
    critical_tol = s.tolerance_mm if s.tolerance_mm < 0.1 else None
    dim((fx, fy), (fx + L * sc, fy), (fx, fy - 15), tol=critical_tol if f.critical == "overall length" else None)
    dim((fx, fy), (fx, fy + H * sc), (fx - 15, fy), angle=90)

    # --- top view (plan) or second view of a turned part
    rect(tx, ty, L * sc, W * sc)
    if turned:
        msp.add_line((tx - 5, ty + W * sc / 2), (tx + L * sc + 5, ty + W * sc / 2), dxfattribs={"layer": "CENTER"})
    else:
        for x, y in f.hole_xy:
            msp.add_circle((tx + x * sc, ty + y * sc), f.hole_dia * sc / 2, dxfattribs={"layer": "OUTLINE"})
        for x, y in f.thread_xy:
            msp.add_circle((tx + x * sc, ty + y * sc), _thread_d(f.thread) * sc / 2, dxfattribs={"layer": "OUTLINE"})
            msp.add_arc((tx + x * sc, ty + y * sc), _thread_d(f.thread) * sc * 0.6, 0, 270, dxfattribs={"layer": "HIDDEN"})
        if s.bends:
            for i in range(s.bends):
                x = tx + L * sc * (i + 1) / (s.bends + 1)
                msp.add_line((x, ty), (x, ty + W * sc), dxfattribs={"layer": "HIDDEN"})
        if len(f.hole_xy) >= 2:
            (x1, y1), (x2, y2) = f.hole_xy[0], f.hole_xy[1]
            if abs(y1 - y2) < 0.01 and abs(x1 - x2) > 0.01:
                dim((tx + x1 * sc, ty + y1 * sc), (tx + x2 * sc, ty + y2 * sc), (tx, ty + W * sc + 12),
                    tol=critical_tol if f.critical == "hole pitch" else None)  # fmt: skip
            else:
                dim((tx + x1 * sc, ty + y1 * sc), (tx + x2 * sc, ty + y2 * sc), (tx + L * sc + 12, ty), angle=90,
                    tol=critical_tol if f.critical == "hole pitch" else None)  # fmt: skip
    dim((tx + L * sc, ty), (tx + L * sc, ty + W * sc), (tx + L * sc + 25, ty), angle=90)

    # --- right / end view
    if turned:
        cx, cy, r = rx + W * sc / 2, ry + H * sc / 2, W * sc / 2
        msp.add_circle((cx, cy), r, dxfattribs={"layer": "OUTLINE"})
        msp.add_line((cx - r - 5, cy), (cx + r + 5, cy), dxfattribs={"layer": "CENTER"})
        msp.add_line((cx, cy - r - 5), (cx, cy + r + 5), dxfattribs={"layer": "CENTER"})
        for x, y in f.hole_xy:
            msp.add_circle((cx + x * sc, cy + y * sc), f.hole_dia * sc / 2, dxfattribs={"layer": "OUTLINE"})
        for x, y in f.thread_xy:
            msp.add_circle((cx + x * sc, cy + y * sc), _thread_d(f.thread) * sc / 2, dxfattribs={"layer": "OUTLINE"})
        dd = msp.add_diameter_dim(center=(cx, cy), radius=r, angle=45, dimstyle="EZDXF", override=dict(dimstyle),
                                  dxfattribs={"layer": "DIM"})  # fmt: skip
        dd.render()
        if f.hole_xy and f.hole_xy[0] == (0.0, 0.0):
            bd = msp.add_diameter_dim(center=(cx, cy), radius=f.hole_dia * sc / 2, angle=200, dimstyle="EZDXF",
                                      override=dict(dimstyle), dxfattribs={"layer": "DIM"})  # fmt: skip
            if critical_tol is not None and f.critical == "bore":
                bd.set_tolerance(critical_tol * unit_factor, critical_tol * unit_factor, hfactor=0.8)
            bd.render()
    else:
        rect(rx, ry, W * sc, H * sc)

    # --- callouts, GD&T, finish
    notes_x, notes_y = 20.0, 56.0
    callouts = []
    if not (turned and f.hole_xy and f.hole_xy[0] == (0.0, 0.0)) and f.hole_xy:
        callouts.append(f"{len(f.hole_xy)}X Ø{fmt_len(f.hole_dia, v.units)} THRU")
    if f.thread:
        tname = INCH_THREAD[f.thread] if v.units == "inch" else f.thread.replace("x", "×")
        callouts.append(f"{len(f.thread_xy)}X {tname} - 6H THRU" if not turned else f"{len(f.thread_xy)}X {tname} - 6H DEPTH 12")
    if s.pockets and not turned:
        callouts.append(f"{s.pockets}X POCKET, R3 MAX INTERNAL CORNERS")
    if s.bends:
        callouts.append(f"{s.bends}X BEND 90° INNER R{fmt_len(max(s.height, 1.0), v.units)}")
    bx = SHEET_W - 185  # callout band, top right
    for i, c in enumerate(callouts):
        _text(msp, c, bx, 276 - i * 6, 3.0)
    if f.gdt:
        tol = f"Ø{s.tolerance_mm / 25.4:.4f}".replace("0.", ".") if v.units == "inch" else f"Ø{s.tolerance_mm:g}"
        _fcf(msp, bx, 245, tol)
        _datum(msp, "A", fx + L * sc * 0.15, fy, down=True)
        _datum(msp, "B", fx + L * sc, fy + H * sc * 0.5, down=False)
    _finish_symbol(msp, SHEET_W - 70, 245, s.ra_um)

    # --- notes + title block
    mat = "SEE PURCHASE ORDER" if v.material_in_email else MATERIAL_NAMES[s.material]
    finish = ", ".join(FINISH_NAMES[o] for o in s.outside) or "NONE"
    gen = "GENERAL TOLERANCES ISO 2768-m" if v.units == "mm" else "GENERAL TOLERANCES ±.005 UNLESS NOTED"
    notes = ["NOTES:", "1. UNLESS OTHERWISE SPECIFIED: " + ("DIMENSIONS IN MM." if v.units == "mm" else "DIMENSIONS IN INCHES."),
             f"2. {gen}.", "3. BREAK ALL SHARP EDGES 0.2 X 45°.", f"4. SURFACE FINISH Ra {s.ra_um:g} µm UNLESS NOTED.",
             f"5. FINISH: {finish}."]  # fmt: skip
    if v.material_in_email:
        notes.append("6. MATERIAL PER PURCHASE ORDER / CUSTOMER SPECIFICATION.")
    for i, n in enumerate(notes):
        _text(msp, n, notes_x, notes_y - i * 5, 2.5)
    _title_block(msp, drawing_id, p, customer, mat, finish, scale_label(sc), v.units, issued, 1, v.sheets)
    rect(10, 10, SHEET_W - 20, SHEET_H - 20, layer="FRAME")

    if v.sheets == 2:  # sheet 2 to the right in model space: a detail + inspection notes
        ox = SHEET_W + 20
        rect(ox + 10, 10, SHEET_W - 20, SHEET_H - 20, layer="FRAME")
        rect(ox + 60, 120, 120, 80)
        _text(msp, "DETAIL A (SCALE 2:1) - EDGE BREAK 0.2 X 45°", ox + 60, 210, 3.5)
        _text(msp, "INSPECTION: FIRST ARTICLE REPORT REQUIRED (FAI).", ox + 30, 90, 3.0)
        _text(msp, f"CRITICAL DIMENSION: {f.critical.upper()} {tol_text(s.tolerance_mm, v.units)}", ox + 30, 82, 3.0)
        _title_block(msp, drawing_id, p, customer, mat, finish, "2:1", v.units, issued, 2, 2, ox=ox)
    return doc


def _thread_d(thread: str) -> float:
    return float(next(d for t, d in THREADS if t == thread)) if thread else 0.0


def _text(msp: Modelspace, s: str, x: float, y: float, h: float, layer: str = "TEXT") -> None:
    msp.add_text(s, height=h, dxfattribs={"layer": layer, "style": "ISO"}).set_placement((x, y))


def _fcf(msp: Modelspace, x: float, y: float, tol: str) -> None:
    """Feature control frame: [position symbol | Ø tol | A | B]. The symbol is drawn as geometry."""
    cells = [8.0, 22.0, 8.0, 8.0]
    cx = x
    for w in cells:
        msp.add_lwpolyline([(cx, y), (cx + w, y), (cx + w, y + 7), (cx, y + 7)], close=True, dxfattribs={"layer": "DIM"})
        cx += w
    msp.add_circle((x + 4, y + 3.5), 2.0, dxfattribs={"layer": "DIM"})
    msp.add_line((x + 1, y + 3.5), (x + 7, y + 3.5), dxfattribs={"layer": "DIM"})
    msp.add_line((x + 4, y + 0.5), (x + 4, y + 6.5), dxfattribs={"layer": "DIM"})
    _text(msp, tol, x + 10, y + 2, 3.0, "DIM")
    _text(msp, "A", x + 32, y + 2, 3.0, "DIM")
    _text(msp, "B", x + 40, y + 2, 3.0, "DIM")
    _text(msp, "POSITION", x, y + 9, 2.0, "DIM")


def _datum(msp: Modelspace, letter: str, x: float, y: float, down: bool) -> None:
    """Datum feature symbol: a boxed letter on a leader from the datum face (below or to the right)."""
    if down:
        bx, by = x - 3, y - 26
        msp.add_line((x, y), (x, by + 6), dxfattribs={"layer": "DIM"})
    else:
        bx, by = x + 6, y - 3
        msp.add_line((x, y), (bx, y), dxfattribs={"layer": "DIM"})
    msp.add_lwpolyline([(bx, by), (bx + 6, by), (bx + 6, by + 6), (bx, by + 6)], close=True, dxfattribs={"layer": "DIM"})
    _text(msp, letter, bx + 1.8, by + 1.5, 3.0, "DIM")


def _finish_symbol(msp: Modelspace, x: float, y: float, ra: float) -> None:
    msp.add_lwpolyline([(x, y + 5), (x + 3, y), (x + 9, y + 10), (x + 22, y + 10)], dxfattribs={"layer": "DIM"})
    _text(msp, f"Ra {ra:g}", x + 9, y + 11, 3.0, "DIM")


def _title_block(msp: Modelspace, drawing_id: str, p: PartRec, customer: str, material: str, finish: str,
                 scale: str, units: str, issued: date, sheet: int, sheets: int, ox: float = 0.0) -> None:  # fmt: skip
    x0, y0, w, h = ox + SHEET_W - 190, 10.0, 180.0, 56.0
    rows = [
        ("CUSTOMER", customer.upper()), ("TITLE", f"{p.label.upper()}"), ("DRAWING NO", p.drawing),
        ("REV", p.rev), ("MATERIAL", material), ("FINISH", finish[:44]),
        ("SCALE", scale), ("UNITS", "MM" if units == "mm" else "INCH"),
        ("DATE", issued.isoformat()), ("SHEET", f"{sheet} OF {sheets}"),
    ]  # fmt: skip
    msp.add_lwpolyline([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)], close=True, dxfattribs={"layer": "FRAME"})
    rh = h / 5
    for i in range(1, 5):
        msp.add_line((x0, y0 + i * rh), (x0 + w, y0 + i * rh), dxfattribs={"layer": "FRAME"})
    msp.add_line((x0 + w / 2, y0), (x0 + w / 2, y0 + h), dxfattribs={"layer": "FRAME"})
    for i, (k, val) in enumerate(rows):
        col, row = i % 2, 4 - i // 2
        cx, cy = x0 + col * w / 2 + 2, y0 + row * rh
        _text(msp, k, cx, cy + rh - 3.2, 1.8)
        _text(msp, val, cx, cy + 2.0, 3.0 if len(val) < 26 else 2.2)
    _text(msp, f"PROPERTY OF {customer.upper()} - CONFIDENTIAL", x0, y0 + h + 2, 2.2)


# ------------------------------------------------------------------ PDF
def dxf_texts(msp: Modelspace) -> list[tuple[str, float, float, float, float]]:
    """(text, x, y, height, rotation) of every visible text, including the text inside dimensions."""
    out = []
    for e in msp:
        ents = list(e.virtual_entities()) if e.dxftype() == "DIMENSION" else [e]
        for t in ents:
            if t.dxftype() == "TEXT":
                out.append((t.plain_text(), t.dxf.insert.x, t.dxf.insert.y, t.dxf.height, t.dxf.get("rotation", 0.0)))
            elif t.dxftype() == "MTEXT":
                txt = t.plain_text(split=False).replace("\n", " ")
                h = t.dxf.char_height
                x, y = t.dxf.insert.x, t.dxf.insert.y
                rot = t.get_rotation()
                if t.dxf.get("attachment_point", 1) in (4, 5, 6):  # middle-attached: shift to baseline-left
                    width = pymupdf.get_text_length(txt, "helv", h) * 0.9
                    if abs(rot) < 1:
                        x, y = x - width / 2, y - h / 2
                    else:
                        x, y = x + h / 2, y - width / 2
                out.append((txt, x, y, h, rot))
    return out


def export_pdf(doc: Drawing, path: Path, sheets: int) -> None:
    msp = doc.modelspace()
    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE,
                               color_policy=config.ColorPolicy.BLACK, lineweight_scaling=1.0)  # fmt: skip
    pdf = pymupdf.open()
    texts = dxf_texts(msp)
    for sheet in range(sheets):
        ox = sheet * (SHEET_W + 20)
        backend = pdf_backend.PyMuPdfBackend()
        Frontend(RenderContext(doc), backend, config=cfg).draw_layout(msp)
        page_def = layout.Page(SHEET_W, SHEET_H, layout.Units.mm, margins=layout.Margins.all(0))
        box = BoundingBox2d([(ox, 0), (ox + SHEET_W, SHEET_H)])
        one = pymupdf.open(
            "pdf", backend.get_pdf_bytes(page_def, settings=layout.Settings(fit_page=False, scale=1.0), render_box=box)
        )
        pdf.insert_pdf(one)
        page = pdf[-1]
        k = 72 / 25.4
        for txt, x, y, h, rot in texts:
            if ox <= x < ox + SHEET_W:
                r = int(round(rot / 90.0)) * 90 % 360
                page.insert_text(((x - ox) * k, (SHEET_H - y) * k), txt, fontsize=h * k * 1.25, fontname="helv",
                                 render_mode=3, rotate=r)  # fmt: skip
    pdf.set_metadata({"title": path.stem, "creator": "Apex legacy CAD export (ezdxf)", "producer": "PyMuPDF"})
    pdf.save(path, garbage=3, deflate=True)
    pdf.close()


# ------------------------------------------------------------------ STEP
def export_step(p: PartRec, f: Features, path: Path) -> tuple[list[float], float]:
    import cadquery as cq

    s = p.spec
    if s.process == "turn":
        solid = cq.Workplane("YZ").circle(s.width / 2).extrude(s.length)
        if f.hole_xy and f.hole_xy[0] == (0.0, 0.0):
            solid = solid.faces(">X").workplane().hole(f.hole_dia)
    elif p.family in ("l_bracket", "u_bracket"):
        t = p.params.get("t", s.height / 5)
        solid = cq.Workplane("XY").box(s.length, s.width, t, centered=False)
        solid = solid.union(cq.Workplane("XY").box(t, s.width, s.height, centered=False))
        if p.family == "u_bracket":
            solid = solid.union(cq.Workplane("XY").box(t, s.width, s.height, centered=False).translate((s.length - t, 0, 0)))
        if f.hole_xy:
            solid = solid.faces("<Z").workplane(centerOption="ProjectedOrigin", origin=(0, 0, 0)).pushPoints(
                [(x, -y) for x, y in f.hole_xy if x > t + f.hole_dia]).hole(f.hole_dia, t)  # fmt: skip
    else:
        solid = cq.Workplane("XY").box(s.length, s.width, s.height, centered=False)
        if f.hole_xy:
            solid = solid.faces(">Z").workplane(centerOption="ProjectedOrigin", origin=(0, 0, 0)).pushPoints(
                f.hole_xy).hole(f.hole_dia)  # fmt: skip
    cq.exporters.export(solid, str(path))
    bb = solid.val().BoundingBox()
    return [round(bb.xlen, 3), round(bb.ylen, 3), round(bb.zlen, 3)], round(solid.val().Volume() / 1000.0, 3)


# ------------------------------------------------------------------ one drawing end to end
def make_drawing(p: PartRec, drawing_id: str, folder: Path, customer: str, issued: date, v: Variant, kind: str,
                 source_part: str | None = None) -> GroundTruth:  # fmt: skip
    folder.mkdir(parents=True, exist_ok=True)
    f = features(p)
    doc = build_dxf(p, f, v, drawing_id, customer, issued)
    files: dict[str, str] = {}
    dxf_path = folder / f"{drawing_id}.dxf"
    doc.saveas(dxf_path)
    files["dxf"] = dxf_path.relative_to(ROOT).as_posix()
    pdf_path = folder / f"{drawing_id}.pdf"
    export_pdf(doc, pdf_path, v.sheets)
    if v.scan:
        from legacy.seed.scan_degrade import degrade

        scan_path = folder / f"{drawing_id}_scan.pdf"
        degrade(pdf_path, scan_path, seed=drawing_id)
        pdf_path.unlink()
        scan_path.rename(pdf_path)
    files["pdf"] = pdf_path.relative_to(ROOT).as_posix()
    gt = GroundTruth(
        drawing_id=drawing_id, part_code=p.code, drawing_no=p.drawing, rev=p.rev, family=p.family,
        customer_key=p.customer_key, material=p.spec.material,
        material_text_on_drawing=None if v.material_in_email else MATERIAL_NAMES[p.spec.material],
        material_in_email=v.material_in_email, units=v.units,
        envelope_mm=[p.spec.length, p.spec.width, p.spec.height],
        holes={"count": len(f.hole_xy), "dia_mm": f.hole_dia}, threads={"count": len(f.thread_xy), "size": f.thread},
        pockets=p.spec.pockets, bends=p.spec.bends, tightest_tolerance_mm=p.spec.tolerance_mm,
        general_tolerance="ISO 2768-m" if v.units == "mm" else "±0.005 in", ra_um=p.spec.ra_um, gdt=f.gdt,
        outside_processes=list(p.spec.outside), sheets=v.sheets, scan=v.scan, kind=kind, source_part=source_part,
        files=files,
    )  # fmt: skip
    if v.step:
        step_path = folder / f"{drawing_id}.step"
        gt.step_bbox_mm, gt.step_volume_cm3 = export_step(p, f, step_path)
        files["step"] = step_path.relative_to(ROOT).as_posix()
    gt_dir = OUT / "ground_truth"
    gt_dir.mkdir(parents=True, exist_ok=True)
    (gt_dir / f"{drawing_id}.json").write_text(json.dumps(asdict(gt), indent=2), encoding="utf-8")
    return gt


# ------------------------------------------------------------------ batch: all history + RFQ drawings
def _job(args: tuple[PartRec, str, str, str, date, Variant, str, str | None]) -> str:
    p, drawing_id, folder, customer, issued, v, kind, source = args
    make_drawing(p, drawing_id, ROOT / folder, customer, issued, v, kind, source)
    return drawing_id


def _done(drawing_id: str) -> bool:
    gt = OUT / "ground_truth" / f"{drawing_id}.json"
    if not gt.exists():
        return False
    files = json.loads(gt.read_text(encoding="utf-8"))["files"]
    return all((ROOT / f).exists() for f in files.values())


def main() -> None:
    import os
    import shutil
    import time
    from concurrent.futures import ProcessPoolExecutor

    from legacy.seed.history import generate
    from legacy.seed.rfq_parts import PROSPECTS, rfq_items

    t0 = time.monotonic()
    h = generate()
    names = {c.key: c.name for c in h.customers} | {k: n for k, n, *_ in PROSPECTS}
    first_quote: dict[str, date] = {}
    for q in h.quotes:
        first_quote.setdefault(q.part_code, q.received_at.date())

    hist = "data/generated/drawings/history"
    jobs: list[tuple[PartRec, str, str, str, date, Variant, str, str | None]] = []
    for p in h.parts.values():
        if not _done(p.code):
            v = Variant(step=random.Random(p.code).random() < 0.3)  # Apex's own archive: clean drawings
            jobs.append((p, p.code, hist, names[p.customer_key], first_quote.get(p.code, h.today), v, "history", None))
    items = rfq_items(h)
    rfq = "data/generated/drawings/rfq"
    copies = [it for it in items if it.kind == "repeat"]
    for it in items:
        if it.kind != "repeat" and not _done(it.rfq_id):
            v = Variant(it.units, it.material_in_email, it.sheets, it.scan, it.step)
            jobs.append((it.part, it.rfq_id, rfq, names[it.customer_key], h.today, v, it.kind, it.source_part))
    workers = max(1, (os.cpu_count() or 2) - 1)
    print(f"drawings: {len(jobs)} to generate on {workers} processes "
          f"({len(h.parts)} history + {len(items)} RFQ in total)")  # fmt: skip
    with ProcessPoolExecutor(workers) as pool:
        for i, _ in enumerate(pool.map(_job, jobs, chunksize=4), 1):
            if i % 50 == 0:
                print(f"  {i}/{len(jobs)} ({time.monotonic() - t0:.0f}s)")

    # Repeats: the customer sends the drawing Apex already has (identical file, or a fresh scan of it).
    from legacy.seed.scan_degrade import degrade

    (ROOT / rfq).mkdir(parents=True, exist_ok=True)
    for it in copies:
        if _done(it.rfq_id):
            continue
        src = json.loads((OUT / "ground_truth" / f"{it.source_part}.json").read_text(encoding="utf-8"))
        files = {}
        for kind, rel in src["files"].items():
            dst = ROOT / rfq / f"{it.rfq_id}{Path(rel).suffix}"
            if kind == "pdf" and it.scan:
                degrade(ROOT / rel, dst, seed=it.rfq_id)
            else:
                shutil.copyfile(ROOT / rel, dst)
            files[kind] = dst.relative_to(ROOT).as_posix()
        src.update(drawing_id=it.rfq_id, kind="repeat", source_part=it.source_part, scan=it.scan, files=files,
                   customer_key=it.customer_key)  # fmt: skip
        (OUT / "ground_truth" / f"{it.rfq_id}.json").write_text(json.dumps(src, indent=2), encoding="utf-8")

    index = [{"rfq_id": it.rfq_id, "customer_key": it.customer_key, "customer_name": names[it.customer_key],
              "part_code": it.part.code, "kind": it.kind, "source_part": it.source_part, "reason": it.reason,
              "units": it.units, "material_in_email": it.material_in_email, "material": it.part.spec.material,
              "sheets": it.sheets, "scan": it.scan, "step": it.step or bool(it.kind == "repeat" and
              (ROOT / rfq / f"{it.rfq_id}.step").exists())} for it in items]  # fmt: skip
    (OUT / "rfq_parts.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    n_step = sum(1 for f in (ROOT / hist).glob("*.step"))
    print(f"drawings done in {time.monotonic() - t0:.0f}s: {len(h.parts)} history ({n_step} with STEP), "
          f"{len(items)} RFQ, index data/generated/rfq_parts.json")  # fmt: skip


if __name__ == "__main__":
    main()
