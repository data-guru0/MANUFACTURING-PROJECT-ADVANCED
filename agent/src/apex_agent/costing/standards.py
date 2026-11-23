"""Apex's shop standards (agent/config/costing.yaml): stock size and standard minutes per operation (S5).

This is the PLANNING BASELINE an estimator would compute. The cycle-time model then learns, from Apex's finished
work orders, how actual times differ from it (per work center, family, material, lot size).
"""

import math
import os
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

CONFIG = Path(os.environ.get("APEX_CONFIG_DIR", "/app/agent-config"))
MAIN = ("MILL3", "MILL5", "LATHE", "LASER")


@cache
def shop() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((CONFIG / "costing.yaml").read_text(encoding="utf-8"))
    return data


@dataclass
class Part:
    """What costing needs to know about a part (from S4's extraction, or from Apex's own drawing of a repeat)."""

    family: str
    material: str
    length: float
    width: float
    height: float
    holes: int = 0
    threads: int = 0
    pockets: int = 0
    bends: int = 0
    tolerance_mm: float = 0.1
    ra_um: float = 3.2
    outside: list[str] = field(default_factory=list)
    finished_volume_cm3: float | None = None  # from a STEP model; else estimated with the family's fill factor
    hole_dia_mm: float = 6.0

    @property
    def process(self) -> str:
        return str(shop()["families"][self.family]["process"])


def family_from_title(title: str | None) -> str | None:
    t = (title or "").upper()
    for fam, f in shop()["families"].items():
        if any(k in t for k in f["title"]):
            return str(fam)
    return None


def _round_up(value: float, sizes: list[float]) -> float:
    for s in sizes:
        if s >= value:
            return float(s)
    return float(math.ceil(value / 5.0) * 5.0)


def _ceil5(v: float) -> float:
    return float(math.ceil(v / 5.0) * 5.0)


def stock(p: Part) -> tuple[tuple[float, float, float], float]:
    """(standard stock size mm, stock volume cm3)."""
    s = shop()["stock"]
    if p.process == "turn":
        d = _round_up(p.width + s["turn_dia_allowance_mm"], s["bar_dia_mm"])
        size = (_ceil5(p.length + s["turn_len_allowance_mm"]), d, d)
        return size, math.pi / 4 * d**2 * size[0] / 1000.0
    if p.process == "sheet":
        m = 2 * s["sheet_margin_mm"]
        size = (_ceil5(p.length + m), _ceil5(p.width + m), _round_up(p.height, s["sheet_mm"]))
    else:
        a = 2 * s["mill_allowance_mm"]
        size = (_ceil5(p.length + a), _ceil5(p.width + a), _round_up(p.height + a, s["plate_mm"]))
    return size, size[0] * size[1] * size[2] / 1000.0


def _factor(table: list[list[float]], value: float) -> float:
    for threshold, f in table:
        if value >= threshold - 1e-9:
            return float(f)
    return float(table[-1][1])


def routing(p: Part) -> list[str]:
    f, five = shop()["families"][p.family], shop()["five_axis_when"]
    r = list(f["routing"])
    if p.family == "sheet_panel" and p.bends:
        r.append("BRAKE")
    if (
        "MILL3" in r
        and r.index("MILL3") == 1
        and (f["setups"] >= five["setups_at_least"] or p.tolerance_mm <= five["tolerance_at_most_mm"])
    ):
        r[1] = "MILL5"
    r.append("DEBURR")
    if p.tolerance_mm <= shop()["times"]["cmm_when_tolerance_mm"]:
        r.append("CMM")
    return r


def planned_minutes(p: Part, fill: float) -> dict[str, float]:
    """Standard minutes PER PART on each work center of the routing (inspection is costed separately)."""
    t, fam = shop()["times"], shop()["families"][p.family]
    tf, rf = _factor(t["tolerance_factor"], p.tolerance_mm), _factor(t["ra_factor"], p.ra_um)
    _, stock_cm3 = stock(p)
    finished = p.finished_volume_cm3 if p.finished_volume_cm3 else fill * p.length * p.width * p.height / 1000.0
    out: dict[str, float] = {}
    r = routing(p)
    main = next(wc for wc in r if wc in MAIN)
    for wc in r:
        if wc == main and p.process != "sheet":
            removed = max(stock_cm3 - finished, 0.0)
            base = removed / t["mrr_cm3_per_min"][p.material] + p.holes * t["min_per_hole"] + p.threads * t["min_per_thread"] \
                + p.pockets * t["min_per_pocket"]  # fmt: skip
            out[wc] = base * tf * rf + fam["setups"] * t["handling_min_per_setup"]
        elif wc in ("MILL3", "MILL5"):  # a secondary milling op (keyway, gear teeth, flats)
            out[wc] = (
                p.pockets * t["min_per_pocket"] + p.holes * t["min_per_hole"] + p.threads * t["min_per_thread"] + 2.0
            ) * tf * rf + 2.0
        elif wc == "LASER":
            cut = 2 * (p.length + p.width) + p.holes * math.pi * p.hole_dia_mm
            speed = t["laser_base_speed_mm_min_x_thickness"] / max(p.height, 0.5) * t["laser_cut_factor"].get(p.material, 0.8)
            out[wc] = (cut / speed + (p.holes + 1) * t["laser_min_per_pierce"]) * tf
        elif wc == "BRAKE":
            out[wc] = p.bends * t["brake_min_per_bend"] * tf
        elif wc == "SAW":
            out[wc] = t["saw_min_per_part"]
        elif wc == "DEBURR":
            out[wc] = t["deburr_min_per_part"] + t["deburr_min_per_feature"] * (p.holes + p.threads)
        elif wc == "CMM":
            out[wc] = 0.0
    return out


def inspection_minutes_per_lot(p: Part, qty: int) -> float:
    t = shop()["times"]
    if p.tolerance_mm > t["cmm_when_tolerance_mm"]:
        return 0.0
    features = p.holes + p.threads + p.pockets + 4
    return float(features * t["cmm_min_per_feature"] * (1 + t["cmm_sample_rate"] * qty))
