"""L2: the legacy cost model, checked against a quote calculated by hand (DEMO.md, L2 theory).

Pure functions, so no services are needed here; the history checks live in test_l2_history.py.
"""

import random

import pytest

from legacy.seed.costing_model import PartSpec, estimate
from legacy.seed.part_families import FAMILIES

# A simple aluminium plate: 100 x 50 x 10 mm, 4 holes, 2 threads, loose tolerance, 10 pcs, tier B.
PLATE = PartSpec("mill", "MAT-AL6061", 100, 50, 10, 50.0, holes=4, threads=2, setups=1,
                 tolerance_mm=0.1, ra_um=3.2, routing=["SAW", "MILL3", "DEBURR"])  # fmt: skip


def test_hand_calculated_quote() -> None:
    b = estimate(PLATE, qty=10, tier="B")
    # 1. Stock: 100+6 -> 110, 50+6 -> 60 (round up to 5 mm), 10+6=16 -> 16 mm plate.
    assert b.stock_mm == (110, 60, 16)
    # 2. Material: 110*60*16/1000 = 105.6 cm³ x 2.70 g/cm³ = 0.28512 kg x $6.50 x 1.08 scrap.
    assert b.stock_kg == pytest.approx(0.28512, abs=1e-4)  # stored to 4 decimals
    assert b.material == pytest.approx(0.28512 * 6.50 * 1.08)  # $2.0015
    # 3. Run: mill (105.6-50)/120 + 4x0.5 + 2x0.8 = 4.0633 min, +2.0 handling = 6.0633 min;
    #    saw 1.5 min; deburr 1.0 + 0.1x6 = 1.6 min.
    assert b.run_min == pytest.approx({"SAW": 1.5, "MILL3": 6.063333, "DEBURR": 1.6})
    run = 6.063333 / 60 * 85 + 1.5 / 60 * 45 + 1.6 / 60 * 40  # $10.78
    assert b.run == pytest.approx(run, rel=1e-5)
    # 4. Setup per lot: mill 45 min @85 + saw 5 @45 + deburr 5 @40 = $70.83, / 10 pcs.
    assert b.setup == pytest.approx((45 / 60 * 85 + 5 / 60 * 45 + 5 / 60 * 40) / 10)
    # 5./6. No CMM, no outside process.
    assert b.inspection == 0 and b.outside == 0
    # 7. Overhead 15 % of (material + setup + run).
    base = b.material + b.setup + b.run
    assert b.overhead == pytest.approx(0.15 * base)
    # 8. Tier B markup 22 % (qty < 100).
    assert b.markup_pct == pytest.approx(0.22)
    assert b.unit_price == 27.87  # = 22.8462 x 1.22, rounded to cents


def test_tighter_tolerance_never_cheaper_and_more_pieces_never_dearer() -> None:
    rng = random.Random(7)
    for _ in range(200):
        part = rng.choice(list(FAMILIES.values()))(rng)
        spec = part.spec
        prices = []
        for tol in [0.1, 0.05, 0.02, 0.01, 0.005]:
            spec.tolerance_mm = tol
            prices.append(estimate(spec, 50, "B").unit_price)
        assert prices == sorted(prices), "a tighter tolerance lowered the price"
        unit = [estimate(spec, q, "B").unit_price for q in [1, 5, 10, 50, 100, 500, 1000, 5000]]
        assert unit == sorted(unit, reverse=True), "a higher quantity raised the unit price"


def test_rejects_zero_quantity() -> None:
    with pytest.raises(ValueError):
        estimate(PLATE, qty=0, tier="B")
