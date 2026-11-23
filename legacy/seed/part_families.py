"""Eight parametric part families Apex makes. Each `make_<family>(rng)` returns a PartSpec
plus a few display attributes. Dimensions in mm, volumes in cm³.

ponytail: geometry is simplified to what costing needs (bounding box, finished volume,
feature counts). L3 draws the full drawing from these same parameters.
"""

import math
import random
from collections.abc import Callable
from dataclasses import dataclass

from legacy.seed.costing_model import PartSpec


@dataclass
class Part:
    family: str
    label: str
    spec: PartSpec
    params: dict[str, float]


TOLERANCES = [0.1, 0.05, 0.02, 0.01, 0.005]
TOL_WEIGHTS = [30, 35, 20, 12, 3]
RAS = [3.2, 1.6, 0.8, 0.4]
RA_WEIGHTS = [45, 35, 15, 5]


def _tol(rng: random.Random) -> float:
    return rng.choices(TOLERANCES, TOL_WEIGHTS)[0]


def _ra(rng: random.Random) -> float:
    return rng.choices(RAS, RA_WEIGHTS)[0]


def _finish(rng: random.Random, material: str, heat_treat_ok: bool = False) -> list[str]:
    out = []
    if material.startswith("MAT-AL") and rng.random() < 0.5:
        out.append("OSP-ANODIZE")
    elif material in ("MAT-1018", "MAT-S235"):
        if heat_treat_ok and rng.random() < 0.3:
            out.append("OSP-HEATTREAT")
        r = rng.random()
        if r < 0.25:
            out.append("OSP-PLATING")
        elif r < 0.45:
            out.append("OSP-POWDER")
    elif material.startswith("MAT-SS") and rng.random() < 0.4:
        out.append("OSP-PASSIVATE")
    return out


def _routing(main: list[str], tol: float, deburr: bool = True) -> list[str]:
    r = list(main)
    if deburr:
        r.append("DEBURR")
    if tol <= 0.02:
        r.append("CMM")
    return r


def l_bracket(rng: random.Random) -> Part:
    m = rng.choice(["MAT-AL6061", "MAT-AL6061", "MAT-S235", "MAT-1018", "MAT-SS304"])
    L, W, H = rng.randint(40, 200), rng.randint(20, 80), rng.randint(30, 120)
    t = rng.choice([5, 6, 8, 10, 12, 16, 20])
    vol = t * (L * W + (H - t) * W) / 1000
    tol = _tol(rng)
    spec = PartSpec("mill", m, L, W, H, vol, holes=rng.randint(2, 8), threads=rng.randint(0, 4),
                    setups=2, tolerance_mm=tol, ra_um=_ra(rng),
                    routing=_routing(["SAW", "MILL3"], tol), outside=_finish(rng, m))  # fmt: skip
    return Part("l_bracket", "L-Bracket", spec, {"L": L, "W": W, "H": H, "t": t})


def u_bracket(rng: random.Random) -> Part:
    m = rng.choice(["MAT-AL6061", "MAT-S235", "MAT-SS304", "MAT-AL7075"])
    L, W, H = rng.randint(50, 250), rng.randint(25, 100), rng.randint(30, 120)
    t = rng.choice([5, 6, 8, 10, 12])
    vol = t * (L * W + 2 * (H - t) * W) / 1000
    tol = _tol(rng)
    spec = PartSpec("mill", m, L, W, H, vol, holes=rng.randint(4, 10), threads=rng.randint(0, 4),
                    setups=2, tolerance_mm=tol, ra_um=_ra(rng),
                    routing=_routing(["SAW", "MILL3"], tol), outside=_finish(rng, m))  # fmt: skip
    return Part("u_bracket", "U-Bracket", spec, {"L": L, "W": W, "H": H, "t": t})


def flanged_housing(rng: random.Random) -> Part:
    m = rng.choice(["MAT-AL6061", "MAT-AL6061", "MAT-AL7075", "MAT-SS316", "MAT-TI64"])
    D, H = rng.randint(60, 300), rng.randint(30, 150)
    vol = math.pi / 4 * D**2 * H / 1000 * rng.uniform(0.35, 0.6)
    tol = rng.choices(TOLERANCES, [5, 25, 35, 25, 10])[0]
    setups = rng.choice([2, 3, 4])
    machine = "MILL5" if setups >= 3 or tol <= 0.01 else "MILL3"
    spec = PartSpec("mill", m, D, D, H, vol, holes=rng.randint(4, 12), threads=rng.randint(4, 8),
                    pockets=rng.randint(1, 3), setups=setups, tolerance_mm=tol, ra_um=_ra(rng),
                    routing=_routing(["SAW", machine], tol), outside=_finish(rng, m))  # fmt: skip
    return Part("flanged_housing", "Flanged Housing", spec, {"D": D, "H": H})


def spur_gear(rng: random.Random) -> Part:
    m = rng.choice(["MAT-1018", "MAT-1018", "MAT-C360", "MAT-POM", "MAT-SS304"])
    od, face = rng.randint(20, 150), rng.randint(8, 40)
    bore = max(6, round(od * rng.uniform(0.2, 0.35)))
    teeth = max(12, round(od / rng.choice([1.0, 1.5, 2.0, 2.5])))
    vol = math.pi / 4 * (od**2 - bore**2) * face / 1000 * 0.85
    tol = rng.choices(TOLERANCES, [5, 30, 40, 20, 5])[0]
    spec = PartSpec("turn", m, face, od, od, vol, holes=1, threads=rng.randint(0, 1),
                    pockets=max(1, teeth // 10), setups=2, tolerance_mm=tol, ra_um=_ra(rng),
                    routing=_routing(["SAW", "LATHE", "MILL3"], tol),
                    outside=_finish(rng, m, heat_treat_ok=True))  # fmt: skip
    return Part("spur_gear", "Spur Gear", spec, {"OD": od, "face": face, "bore": bore, "teeth": teeth})


def keyed_shaft(rng: random.Random) -> Part:
    m = rng.choice(["MAT-1018", "MAT-1018", "MAT-SS304", "MAT-SS316", "MAT-AL6061"])
    d, L = rng.randint(10, 60), rng.randint(50, 400)
    vol = math.pi / 4 * d**2 * L / 1000 * 0.95
    tol = rng.choices(TOLERANCES, [10, 30, 35, 20, 5])[0]
    spec = PartSpec("turn", m, L, d, d, vol, holes=rng.randint(0, 2), threads=rng.randint(0, 2),
                    pockets=1, setups=2, tolerance_mm=tol, ra_um=_ra(rng),
                    routing=_routing(["SAW", "LATHE", "MILL3"], tol),
                    outside=_finish(rng, m, heat_treat_ok=True))  # fmt: skip
    return Part("keyed_shaft", "Shaft with Keyway", spec, {"d": d, "L": L})


def spacer(rng: random.Random) -> Part:
    m = rng.choice(["MAT-AL6061", "MAT-SS304", "MAT-POM", "MAT-C360", "MAT-1018"])
    od, L = rng.randint(8, 50), rng.randint(5, 60)
    bore = max(3, round(od * rng.uniform(0.3, 0.6)))
    vol = math.pi / 4 * (od**2 - bore**2) * L / 1000
    tol = rng.choices(TOLERANCES, [40, 35, 20, 5, 0])[0]
    spec = PartSpec("turn", m, L, od, od, vol, holes=1, threads=rng.choice([0, 0, 1]), setups=1,
                    tolerance_mm=tol, ra_um=_ra(rng), routing=_routing(["SAW", "LATHE"], tol),
                    outside=_finish(rng, m))  # fmt: skip
    return Part("spacer", "Spacer", spec, {"OD": od, "L": L, "bore": bore})


def mounting_plate(rng: random.Random) -> Part:
    m = rng.choice(["MAT-AL6061", "MAT-AL6061", "MAT-S235", "MAT-SS304", "MAT-AL7075"])
    L, W, t = rng.randint(80, 400), rng.randint(60, 300), rng.choice([6, 8, 10, 12, 16, 20, 25])
    pockets = rng.randint(0, 2)
    vol = L * W * t / 1000 * (1 - 0.08 * pockets)
    tol = _tol(rng)
    spec = PartSpec("mill", m, L, W, t, vol, holes=rng.randint(4, 20), threads=rng.randint(0, 8),
                    pockets=pockets, setups=rng.choice([1, 2]), tolerance_mm=tol, ra_um=_ra(rng),
                    routing=_routing(["SAW", "MILL3"], tol), outside=_finish(rng, m))  # fmt: skip
    return Part("mounting_plate", "Mounting Plate", spec, {"L": L, "W": W, "t": t})


def sheet_panel(rng: random.Random) -> Part:
    m = rng.choice(["MAT-S235", "MAT-S235", "MAT-SS304", "MAT-AL6061"])
    L, W = rng.randint(100, 1200), rng.randint(80, 800)
    t = rng.choice([1.0, 1.5, 2.0, 3.0])
    holes, bends = rng.randint(4, 30), rng.randint(0, 4)
    cut = 2 * (L + W) + holes * math.pi * 6
    vol = L * W * t / 1000
    tol = rng.choices(TOLERANCES, [70, 30, 0, 0, 0])[0]
    main = ["LASER", "BRAKE"] if bends else ["LASER"]
    outside = ["OSP-POWDER"] if m == "MAT-S235" and rng.random() < 0.6 else _finish(rng, m)
    spec = PartSpec("sheet", m, L, W, t, vol, holes=holes, bends=bends, cut_length_mm=cut,
                    setups=1, tolerance_mm=tol, ra_um=3.2, routing=_routing(main, tol),
                    outside=outside)  # fmt: skip
    return Part("sheet_panel", "Sheet-Metal Panel", spec, {"L": L, "W": W, "t": t, "bends": bends})


FAMILIES: dict[str, Callable[[random.Random], Part]] = {
    "l_bracket": l_bracket,
    "u_bracket": u_bracket,
    "flanged_housing": flanged_housing,
    "spur_gear": spur_gear,
    "keyed_shaft": keyed_shaft,
    "spacer": spacer,
    "mounting_plate": mounting_plate,
    "sheet_panel": sheet_panel,
}
# How often each family shows up in RFQs.
FAMILY_WEIGHTS = [16, 12, 10, 10, 12, 14, 14, 12]
# Typical lot sizes per family.
QTY_CHOICES = {
    "l_bracket": [10, 25, 50, 100, 250, 500],
    "u_bracket": [10, 25, 50, 100, 250],
    "flanged_housing": [1, 2, 5, 10, 25, 50],
    "spur_gear": [10, 25, 50, 100, 250, 500],
    "keyed_shaft": [5, 10, 25, 50, 100],
    "spacer": [100, 250, 500, 1000, 2500, 5000],
    "mounting_plate": [1, 5, 10, 25, 50, 100],
    "sheet_panel": [10, 25, 50, 100, 250, 500],
}
