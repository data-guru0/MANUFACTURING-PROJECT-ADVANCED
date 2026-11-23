"""The parts that will arrive in FUTURE RFQs (L5 emails them). Deterministic, files only:
these parts are not in Odoo yet, because nobody has quoted them.

80 items:
- 50 new parts (40 from existing customers, 10 from 3 prospects who never bought from Apex);
- 10 repeats of parts Apex already quoted (W10); 7 are the identical file, 3 were re-scanned;
- 12 revisions (next letter) of parts Apex already quoted (W10);
- 8 out-of-capability parts (too big for any machine, tighter than any machine holds, or an
  unknown material); S11 must say "no bid".
Variety on top (the RFQ drawings only): ~25 % scans, ~15 % inch, ~10 % material only in the email,
~10 % two sheets, ~30 % with a STEP model.
"""

import random
from dataclasses import dataclass

from legacy.seed.history import History, PartRec, _register, _revision
from legacy.seed.part_families import FAMILIES, FAMILY_WEIGHTS

SEED = 20261120
PROSPECTS = [
    ("PROS-01", "Orbital Pump Technologies", "US", "English"),
    ("PROS-02", "Feinmechanik Brandt", "DE", "German"),
    ("PROS-03", "Nexo Robotica", "ES", "Spanish"),
]


@dataclass
class RfqItem:
    rfq_id: str  # RFQ-001 ...
    part: PartRec
    customer_key: str
    kind: str  # new | repeat | revision | out_of_capability
    source_part: str | None
    reason: str = ""  # why out of capability
    units: str = "mm"
    material_in_email: bool = False
    sheets: int = 1
    scan: bool = False
    step: bool = False


def rfq_items(h: History) -> list[RfqItem]:
    rng = random.Random(SEED)
    real = [c for c in h.customers if c.duplicate_of is None]
    quoted = sorted({q.part_code for q in h.quotes})
    parts: dict[str, PartRec] = {}
    items: list[RfqItem] = []
    next_no = [20001]

    def new(cust_key: str, family: str | None = None) -> PartRec:
        fam = family or rng.choices(list(FAMILIES), FAMILY_WEIGHTS)[0]
        drawing = f"D-{next_no[0]}"
        next_no[0] += 1
        return _register(parts, FAMILIES[fam](rng), drawing, "A", cust_key, rng)

    for _ in range(40):
        c = rng.choice(real)
        items.append(RfqItem("", new(c.key), c.key, "new", None))
    for _ in range(10):
        key = rng.choice(PROSPECTS)[0]
        items.append(RfqItem("", new(key), key, "new", None))
    for code in rng.sample(quoted, 10):
        p = h.parts[code]
        items.append(RfqItem("", p, p.customer_key, "repeat", code))
    revisable = [c for c in quoted if h.parts[c].rev in ("A", "B")]
    for code in rng.sample(revisable, 12):
        a = h.parts[code]
        nxt = chr(ord(max(r.rev for r in h.parts.values() if r.drawing == a.drawing)) + 1)
        items.append(RfqItem("", _revision(parts, a, rng, rev=nxt), a.customer_key, "revision", code))
    for i in range(8):
        c = rng.choice(real)
        p = new(c.key, "mounting_plate" if i % 3 == 0 else "flanged_housing")
        if i % 3 == 0:
            p.spec.length, reason = float(rng.randint(900, 1400)), "longer than any machine's X travel (762 mm)"
        elif i % 3 == 1:
            p.spec.tolerance_mm, reason = 0.002, "tolerance 0.002 mm tighter than the best machine (0.005 mm)"
        else:
            p.spec.material, reason = "MAT-TI64", "Ti-6Al-4V with 0.002 mm tolerance: no capable machine"
            p.spec.tolerance_mm = 0.002
        items.append(RfqItem("", p, c.key, "out_of_capability", None, reason))

    rng.shuffle(items)
    for i, it in enumerate(items, 1):
        it.rfq_id = f"RFQ-{i:03d}"
    # Variety. Keep 7 repeats byte-identical to the archived drawing (a file-hash match for S12).
    repeats = [it for it in items if it.kind == "repeat"]
    for it in repeats[7:]:
        it.scan = True
    others = [it for it in items if it.kind != "repeat"]
    for it in rng.sample(others, 20 - 3):
        it.scan = True
    for it in rng.sample(others, 12):
        it.units = "inch"
    for it in rng.sample(others, 8):
        it.material_in_email = True
    for it in rng.sample(others, 8):
        it.sheets = 2
    for it in others:
        it.step = rng.random() < 0.3
    return items
