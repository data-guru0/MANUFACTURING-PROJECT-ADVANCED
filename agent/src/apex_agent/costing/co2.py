"""CO2 estimate per part and per lot (S5, W15). Deterministic; every factor cited (agent/config/emission_factors.yaml).

  material  stock kg (incl. scrap) x cradle-to-gate factor of the material
  energy    sum over operations: P50 hours per part x rated power kW (Odoo x_power_kw) x grid factor
  outside   per-part factor of the process, where one is known
A factor that is null is EXCLUDED and named in `excluded`, so the reviewer can ask the supplier: never guessed.
"""

from dataclasses import dataclass, field
from functools import cache
from typing import Any

import yaml

from apex_agent.costing.standards import CONFIG


@cache
def factors() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((CONFIG / "emission_factors.yaml").read_text(encoding="utf-8"))
    return data


@dataclass
class Footprint:
    per_part_kg: float
    per_lot_kg: float
    lines: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    methodology: str = ""


def footprint(material: str, stock_kg: float, scrap: float, minutes_p50: dict[str, float], power_kw: dict[str, float],
              outside: list[str], qty: int) -> Footprint:  # fmt: skip
    f = factors()
    lines: list[dict[str, Any]] = []
    excluded: list[str] = []
    m = f["materials"].get(material) or {"factor": None, "source": "not in emission_factors.yaml"}
    bought = stock_kg * scrap
    if m["factor"] is None:
        excluded.append(f"material {material}: {m['source']} ({m.get('note', 'ask the supplier')})")
    else:
        lines.append({"name": f"material {material}", "kg_co2e": bought * m["factor"],
                      "calc": f"{bought:.3f} kg bought (stock {stock_kg:.3f} kg x scrap {scrap}) x {m['factor']} kg CO2e/kg",
                      "source": m["source"], "status": m["status"], "year": m.get("year")})  # fmt: skip
    g = f["grid"]
    kwh = sum(minutes_p50[wc] / 60.0 * float(power_kw.get(wc) or 0) for wc in minutes_p50)
    lines.append({"name": "machine energy", "kg_co2e": kwh * g["factor"],
                  "calc": " + ".join(f"{minutes_p50[wc]:.2f} min x {power_kw.get(wc) or 0} kW ({wc})" for wc in minutes_p50)
                          + f" = {kwh:.3f} kWh x {g['factor']} kg CO2e/kWh ({g['region']} grid)",
                  "source": g["source"], "status": g["status"], "year": g.get("year")})  # fmt: skip
    for osp in outside:
        o = f["outside_processes"].get(osp) or {"factor": None, "source": "not in emission_factors.yaml"}
        if o["factor"] is None:
            excluded.append(f"{osp}: {o['source']} ({o.get('note', 'ask the supplier')})")
        else:
            lines.append(
                {"name": osp, "kg_co2e": float(o["factor"]), "calc": f"{o['factor']} kg CO2e per part", "source": o["source"]}
            )
    per_part = sum(ln["kg_co2e"] for ln in lines)
    for ln in lines:
        ln["kg_co2e"] = round(ln["kg_co2e"], 4)
    return Footprint(round(per_part, 4), round(per_part * qty, 2), lines, excluded, f["methodology"].strip())
