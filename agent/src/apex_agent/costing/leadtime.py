"""Delivery date (S5, W7): when can Apex really ship this lot? Deterministic.

1. Material ready: today if it is on hand, else after the supplier's lead time (Odoo price list).
2. Greedy forward-loading: every work center first works off its open backlog (open work orders, Odoo), on its own
   shift calendar; then this lot's operations run in routing order, each starting when the previous one is done.
3. Outside processes (anodising...) after machining, with the finisher's lead time; then the policy buffer.
P50 date = P50 minutes; COMMITTED date = P90 minutes + buffer; RUSH = overtime capacity (policy) at P50.
ponytail: greedy, day-resolution, one job at a time against the backlog (no re-sequencing). Upgrade: OR-Tools CP-SAT
over all open orders if the backtest falls short.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any


@dataclass
class Plan:
    p50: date
    committed: date
    rush: date
    bottleneck: str
    material_ready: date
    steps: list[dict[str, Any]]


class Calendar:
    """Minutes still free per work center per day, consumed as work is loaded."""

    def __init__(self, hours_by_weekday: dict[str, dict[str, float]], capacity: float = 1.0) -> None:
        self.cap = {wc: {int(d): h * 60 * capacity for d, h in days.items()} for wc, days in hours_by_weekday.items()}
        self.used: dict[tuple[str, date], float] = {}

    def load(self, wc: str, start: date, minutes: float) -> date:
        """Put `minutes` on `wc` from `start` on; the day the work finishes."""
        day, left = start, minutes
        for _ in range(3650):
            free = self.cap.get(wc, {}).get(day.weekday(), 0.0) - self.used.get((wc, day), 0.0)
            if free > 0:
                take = min(free, left)
                self.used[(wc, day)] = self.used.get((wc, day), 0.0) + take
                left -= take
                if left <= 1e-9:
                    return day
            day += timedelta(days=1)
        raise ValueError(f"{wc} has no capacity in its calendar")


def _run(route: list[str], lot_min: dict[str, float], today: date, backlog: dict[str, float],
         hours: dict[str, dict[str, float]], ready: date, capacity: float) -> tuple[date, list[dict[str, Any]]]:  # fmt: skip
    cal = Calendar(hours, capacity)
    for wc, minutes in backlog.items():  # the open work orders come first
        if wc in cal.cap and minutes > 0:
            cal.load(wc, today, minutes)
    t, steps = ready, []
    for wc in route:
        finish = cal.load(wc, t, lot_min[wc]) if lot_min[wc] > 0 else t
        steps.append(
            {
                "wc": wc,
                "from": t.isoformat(),
                "to": finish.isoformat(),
                "days": (finish - t).days,
                "minutes": round(lot_min[wc], 1),
            }
        )
        t = finish
    return t, steps


def plan(route: list[str], per_part: dict[str, tuple[float, float, float]], qty: int, today: date, workload: dict[str, Any],
         material_ready: date, outside_days: int, policy: dict[str, Any]) -> Plan:  # fmt: skip
    """per_part: P10/P50/P90 minutes per part by work center (setup included, from the engine)."""
    hours, backlog = workload["hours_by_weekday"], workload["open_minutes"]
    p50_lot = {wc: per_part[wc][1] * qty for wc in route}
    p90_lot = {wc: per_part[wc][2] * qty for wc in route}
    end50, steps = _run(route, p50_lot, today, backlog, hours, material_ready, 1.0)
    end90, _ = _run(route, p90_lot, today, backlog, hours, material_ready, 1.0)
    rush_end, _ = _run(route, p50_lot, today, backlog, hours, material_ready, float(policy["rush_overtime_capacity"]))
    tail = timedelta(days=outside_days)
    buffer = timedelta(days=int(policy["lead_buffer_days"]))
    bottleneck = max(steps, key=lambda s: s["days"])["wc"] if steps else "-"
    return Plan(end50 + tail, end90 + tail + buffer, rush_end + tail, bottleneck, material_ready, steps)


def material_ready(on_hand: float, needed: float, lead_days: int, today: date) -> date:
    return today if on_hand >= needed else today + timedelta(days=lead_days)
