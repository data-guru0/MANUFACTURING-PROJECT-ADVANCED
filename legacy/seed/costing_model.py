"""Apex's LEGACY cost logic: the formula Ramesh's spreadsheet implements.

This is how a job shop prices a machined or sheet-metal part. Every step is
deterministic, and L4's Excel calculator reproduces it cell by cell (within 0.5 %).

1. Stock size     bounding box + allowance, rounded UP to a standard size
                  (plate thickness, bar diameter or sheet gauge; other sides to 5 mm).
2. Material       stock volume x density x $/kg x scrap factor.
3. Cycle time     per part, on the main machine:
                  removed volume / removal rate + holes x 0.5 + threads x 0.8 + pockets x 3.0 min,
                  x tolerance factor x finish factor, + 2.0 min handling per setup.
                  Sheet metal: cut length / cut speed + pierces x 0.05 min on the laser, bends x 0.5 min.
                  Saw 1.5 min/part, deburr 1.0 + 0.1 x (holes + threads) min/part.
4. Setup          setup minutes x rate, once per lot (main machine: x number of setups),
                  amortized over the quantity.
5. Inspection     CMM: (holes + threads + pockets + 4) x 0.5 min on (1 + 10 % of the lot) parts,
                  only when the tightest tolerance is <= 0.02 mm.
6. Outside proc.  max(price x qty, lot minimum) / qty.
7. Overhead       15 % on everything except outside processes.
8. Margin         markup by customer tier (A 15 %, B 22 %, C 30 %), minus 3 points at qty >= 100
                  and 6 points at qty >= 1000, never below 8 %.

The rates and material prices are the L1 Odoo values (one source of truth: setup_odoo.py).
"""

import math
from dataclasses import dataclass, field

from legacy.odoo.setup_odoo import MATERIALS, OUTSIDE_PROCESSES, WORKCENTERS

RATE = {wc[1]: float(wc[2]) for wc in WORKCENTERS}  # $/h by work-center code
SETUP_MIN = {wc[1]: float(wc[3]) for wc in WORKCENTERS}
DENSITY = {m[0]: m[3] for m in MATERIALS}  # g/cm³
PRICE_PER_KG = {m[0]: m[4] for m in MATERIALS}  # today's $/kg
OSP_PRICE = {p[0]: p[2] for p in OUTSIDE_PROCESSES}  # $/part
OSP_LOT_MIN = {
    "OSP-ANODIZE": 150.0,
    "OSP-PLATING": 120.0,
    "OSP-POWDER": 200.0,
    "OSP-PASSIVATE": 120.0,
    "OSP-HEATTREAT": 180.0,
}
# Material removal rate, cm³/min (milling/turning, typical for a small shop).
MRR = {
    "MAT-AL6061": 120.0,
    "MAT-AL7075": 100.0,
    "MAT-SS304": 25.0,
    "MAT-SS316": 20.0,
    "MAT-1018": 40.0,
    "MAT-S235": 45.0,
    "MAT-C360": 140.0,
    "MAT-POM": 180.0,
    "MAT-TI64": 15.0,
}
# Laser cut-speed multiplier vs mild steel.
CUT_FACTOR = {"MAT-S235": 1.0, "MAT-1018": 1.0, "MAT-SS304": 0.7, "MAT-SS316": 0.7, "MAT-AL6061": 0.8}

PLATE_MM = [3, 5, 6, 8, 10, 12, 16, 20, 25, 30, 40, 50, 60, 80, 100, 120, 150]
BAR_DIA_MM = [10, 12, 16, 20, 25, 30, 35, 40, 50, 60, 70, 80, 100, 120, 150, 160]
SHEET_MM = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0]
MILL_ALLOWANCE_MM = 3.0  # per side
TURN_DIA_ALLOWANCE_MM = 3.0
TURN_LEN_ALLOWANCE_MM = 5.0
SHEET_MARGIN_MM = 10.0  # per side, nesting skeleton
SCRAP = {"mill": 1.08, "turn": 1.08, "sheet": 1.15}
TOL_FACTOR = [(0.1, 1.0), (0.05, 1.15), (0.02, 1.35), (0.01, 1.6), (0.005, 2.2), (0.0, 3.0)]
RA_FACTOR = [(3.2, 1.0), (1.6, 1.15), (0.8, 1.35), (0.4, 1.7), (0.0, 2.2)]
OVERHEAD = 0.15
TIER_MARKUP = {"A": 0.15, "B": 0.22, "C": 0.30}
MIN_MARKUP = 0.08


@dataclass
class PartSpec:
    """What an estimator reads off the drawing. Dimensions in mm, finished volume in cm³.

    process: "mill" (block stock), "turn" (bar stock: width = height = diameter) or "sheet"
    (length x width = flat blank, height = sheet thickness).
    routing: work-center codes in order; the main machine is the first of MILL3/MILL5/LATHE/LASER.
    """

    process: str
    material: str
    length: float
    width: float
    height: float
    finished_volume_cm3: float
    holes: int = 0
    threads: int = 0
    pockets: int = 0
    bends: int = 0
    cut_length_mm: float = 0.0
    setups: int = 1
    tolerance_mm: float = 0.1
    ra_um: float = 3.2
    routing: list[str] = field(default_factory=list)
    outside: list[str] = field(default_factory=list)


@dataclass
class CostBreakdown:
    stock_mm: tuple[float, float, float]
    stock_kg: float
    material: float  # $/part
    setup: float  # $/part (amortized)
    run: float  # $/part
    inspection: float  # $/part
    outside: float  # $/part
    overhead: float  # $/part
    markup_pct: float
    unit_cost: float
    unit_price: float
    run_min: dict[str, float]  # minutes per part by work center (estimate)
    lines: dict[str, float] = field(default_factory=dict)


def round_up(value: float, sizes: list[float] | list[int]) -> float:
    """Smallest standard size >= value (or the value rounded up to 5 mm beyond the list)."""
    for s in sizes:
        if s >= value:
            return float(s)
    return float(math.ceil(value / 5.0) * 5.0)


def ceil5(value: float) -> float:
    return float(math.ceil(value / 5.0) * 5.0)


def factor(table: list[tuple[float, float]], value: float) -> float:
    """First factor whose threshold the value reaches (tables go loose -> tight)."""
    for threshold, f in table:
        if value >= threshold - 1e-9:
            return f
    return table[-1][1]


def stock_size(p: PartSpec) -> tuple[float, float, float]:
    if p.process == "turn":
        d = round_up(p.width + TURN_DIA_ALLOWANCE_MM, BAR_DIA_MM)
        return (ceil5(p.length + TURN_LEN_ALLOWANCE_MM), d, d)
    if p.process == "sheet":
        m = 2 * SHEET_MARGIN_MM
        return (ceil5(p.length + m), ceil5(p.width + m), round_up(p.height, SHEET_MM))
    a = 2 * MILL_ALLOWANCE_MM
    return (ceil5(p.length + a), ceil5(p.width + a), round_up(p.height + a, PLATE_MM))


def stock_volume_cm3(p: PartSpec, s: tuple[float, float, float]) -> float:
    if p.process == "turn":
        return math.pi / 4 * s[1] ** 2 * s[0] / 1000.0
    return s[0] * s[1] * s[2] / 1000.0


def main_machine(p: PartSpec) -> str:
    return next(wc for wc in p.routing if wc in ("MILL3", "MILL5", "LATHE", "LASER"))


def run_minutes(p: PartSpec, stock_cm3: float) -> dict[str, float]:
    """Estimated minutes per part on each work center of the routing."""
    tf, rf = factor(TOL_FACTOR, p.tolerance_mm), factor(RA_FACTOR, p.ra_um)
    out: dict[str, float] = {}
    main = main_machine(p)
    for wc in p.routing:
        if wc == main and p.process != "sheet":
            removed = max(stock_cm3 - p.finished_volume_cm3, 0.0)
            base = removed / MRR[p.material] + p.holes * 0.5 + p.threads * 0.8 + p.pockets * 3.0
            out[wc] = base * tf * rf + p.setups * 2.0
        elif wc in ("MILL3", "MILL5"):  # secondary milling op (gear teeth, keyway, flats)
            out[wc] = (p.pockets * 3.0 + p.holes * 0.5 + p.threads * 0.8 + 2.0) * tf * rf + 2.0
        elif wc == "LASER":
            speed = 6000.0 / p.height * CUT_FACTOR.get(p.material, 0.8)  # mm/min
            out[wc] = (p.cut_length_mm / speed + (p.holes + 1) * 0.05) * tf
        elif wc == "BRAKE":
            out[wc] = p.bends * 0.5 * tf
        elif wc == "SAW":
            out[wc] = 1.5
        elif wc == "DEBURR":
            out[wc] = 1.0 + 0.1 * (p.holes + p.threads)
        elif wc == "CMM":
            out[wc] = 0.0  # inspection is costed separately (step 5)
    return out


def markup(tier: str, qty: int) -> float:
    m = TIER_MARKUP[tier]
    if qty >= 1000:
        m -= 0.06
    elif qty >= 100:
        m -= 0.03
    return max(m, MIN_MARKUP)


def estimate(p: PartSpec, qty: int, tier: str, price_per_kg: float | None = None) -> CostBreakdown:
    """The legacy quote for `qty` parts for a customer of `tier`.

    price_per_kg: the material price on the quote date (defaults to today's Odoo price).
    """
    if qty < 1:
        raise ValueError("qty must be >= 1")
    s = stock_size(p)
    vol = stock_volume_cm3(p, s)
    kg = vol * DENSITY[p.material] / 1000.0
    ppk = PRICE_PER_KG[p.material] if price_per_kg is None else price_per_kg
    material = kg * ppk * SCRAP[p.process]

    run_min = run_minutes(p, vol)
    run = sum(run_min[wc] / 60.0 * RATE[wc] for wc in run_min)

    main = main_machine(p)
    setup_lot = sum(SETUP_MIN[wc] / 60.0 * RATE[wc] * (p.setups if wc == main else 1) for wc in p.routing)
    setup = setup_lot / qty

    inspection = 0.0
    if "CMM" in p.routing and p.tolerance_mm <= 0.02:
        features = p.holes + p.threads + p.pockets + 4
        inspection = features * 0.5 / 60.0 * RATE["CMM"] * (1.0 / qty + 0.1)

    outside = sum(max(OSP_PRICE[o] * qty, OSP_LOT_MIN[o]) / qty for o in p.outside)
    overhead = OVERHEAD * (material + setup + run + inspection)
    unit_cost = material + setup + run + inspection + outside + overhead
    mk = markup(tier, qty)
    unit_price = round(unit_cost * (1 + mk), 2)
    return CostBreakdown(
        stock_mm=s,
        stock_kg=round(kg, 4),
        material=material,
        setup=setup,
        run=run,
        inspection=inspection,
        outside=outside,
        overhead=overhead,
        markup_pct=mk,
        unit_cost=unit_cost,
        unit_price=unit_price,
        run_min=run_min,
        lines={
            "material": material,
            "setup": setup,
            "run": run,
            "inspection": inspection,
            "outside": outside,
            "overhead": overhead,
        },
    )
