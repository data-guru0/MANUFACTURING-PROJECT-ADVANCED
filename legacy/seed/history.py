"""Generate Apex's last 3 years of business, deterministically (fixed seed), as plain data.

Nothing here touches Odoo; `load_odoo.py` writes it through the API. All dates are
relative to `today`, so the history always ends "now".

What gets generated, and the weaknesses baked into it:
- 35 customers (+5 duplicate records: W4), tiers A/B/C, NDA / certificate / language flags.
- 12 suppliers with price-list entries, many older than 6 months (W13).
- ~400 RFQs -> quotes priced by `costing_model` at the material price of the day,
  plus human noise (sigma ~10 %) and occasional "Ramesh override" notes.
  Repeat parts re-quoted from scratch (W10) and ~40 Rev A -> Rev B pairs.
- Win probability falls with response delay and price; ~25-30 % won.
  Only ~20 % of lost quotes carry a lost reason (W9).
- Every won order promises "3 weeks" regardless of load (W7); ~70 % deliver on time.
- ~10 quotes accepted months later at the old price after a metal price spike: loss (W8).
- A few out-of-capability jobs outsourced at a loss (W11).
- Completed jobs record ACTUAL work-order times and scrap that differ from the estimate (W14).
- Material grade typed inconsistently (W4); ~15 % of parts have no routing (W4).
- Open + blanket manufacturing orders load the main machines to 60-90 % for 6 weeks.
"""

import math
import random
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from legacy.seed.costing_model import (
    OVERHEAD,
    PRICE_PER_KG,
    RATE,
    SETUP_MIN,
    CostBreakdown,
    PartSpec,
    estimate,
)
from legacy.seed.part_families import FAMILIES, FAMILY_WEIGHTS, QTY_CHOICES, Part

SEED = 20231120
N_QUOTES = 400
YEARS = 3
PROMISE_DAYS = 21  # W7: "usually 3 weeks", no capacity check
CAPACITY_MIN_6W = 6 * 5 * 16 * 60  # 2 shifts x 8 h x 5 days x 6 weeks, per work center
LOAD_TARGET_WCS = ["MILL3", "MILL5", "LATHE", "LASER"]

CUSTOMER_NAMES = [
    "Kestrel Automation", "Northbridge Pumps", "Valdera Robotics", "Halcyon Medical Devices",
    "Ironleaf Agritech", "Corvane Hydraulics", "Bluefin Marine Systems", "Tessaract Optics",
    "Granite Peak Mining Equipment", "Sundial Solar Mounts", "Arcwell Welding Systems",
    "Pinecrest Packaging Machines", "Mariner Valve Co", "Quillon Aerospace Components",
    "Rookfield Rail Systems", "Silverline Food Equipment", "Thornbury Test Rigs",
    "Upland Drone Works", "Vectra Motion Control", "Wexford Instruments", "Yarrow Lab Automation",
    "Zephyr HVAC Products", "Ambrose Printing Systems", "Brightwater Filtration",
    "Cobalt Energy Storage", "Delmont Conveyor Systems", "Everhart Defense Electronics",
    "Foxglove Bio Instruments", "Gantry Lift Solutions", "Harrow Tractor Parts",
    "Krämer Antriebstechnik", "Maschinenbau Lindqvist", "Talleres Ortega", "Industrias Montoya",
    "Bharat Precision Motors",
]  # fmt: skip
CUSTOMER_COUNTRY = {"Krämer Antriebstechnik": ("DE", "German"), "Maschinenbau Lindqvist": ("DE", "German"),
                    "Talleres Ortega": ("ES", "Spanish"), "Industrias Montoya": ("MX", "Spanish"),
                    "Bharat Precision Motors": ("IN", "Hindi")}  # fmt: skip
# W4: the same customer entered twice, written differently.
DUPLICATES = {
    "Kestrel Automation": "Kestrel Automation Inc.",
    "Northbridge Pumps": "NorthBridge Pumps Ltd",
    "Corvane Hydraulics": "Corvane Hydraulic",
    "Vectra Motion Control": "VECTRA Motion-Control",
    "Krämer Antriebstechnik": "Kraemer Antriebstechnik GmbH",
}
CERTS = ["", "", "", "ISO 9001", "ISO 9001", "AS9100", "ISO 13485", "PPAP", "ISO 9001, PPAP"]

SUPPLIERS = [
    ("Midwest Metals Supply", "metal", ["MAT-AL6061", "MAT-AL7075", "MAT-1018", "MAT-S235"]),
    ("Coastal Alloys", "metal", ["MAT-SS304", "MAT-SS316", "MAT-TI64", "MAT-AL6061"]),
    ("Keystone Bar & Plate", "metal", ["MAT-1018", "MAT-S235", "MAT-SS304", "MAT-C360"]),
    ("Summit Steel Service", "metal", ["MAT-S235", "MAT-1018"]),
    ("TitanSource Aerospace Metals", "metal", ["MAT-TI64", "MAT-AL7075"]),
    ("PolyStock Plastics", "metal", ["MAT-POM"]),
    ("Brightline Anodizing", "osp", ["OSP-ANODIZE"]),
    ("Metro Plating Works", "osp", ["OSP-PLATING", "OSP-PASSIVATE"]),
    ("ColorCoat Powder", "osp", ["OSP-POWDER"]),
    ("Heatwave Thermal Processing", "osp", ["OSP-HEATTREAT"]),
    ("Ironclad Finishing", "osp", ["OSP-POWDER", "OSP-PLATING"]),
    ("Precision Partners Machining", "subcon", []),
]
SUBCONTRACTOR = "SUP-12"

GRADE_VARIANTS = {
    "MAT-AL6061": ["Al 6061-T6", "AL6061", "6061-T6", "Alu 6061", "6061 T6", "Aluminium 6061"],
    "MAT-AL7075": ["Al 7075-T6", "7075-T6", "AL7075", "Alu 7075"],
    "MAT-SS304": ["SS304", "304 SS", "AISI 304", "1.4301", "Stainless 304"],
    "MAT-SS316": ["SS316", "316L", "AISI 316", "1.4401"],
    "MAT-1018": ["Steel 1018", "1018", "C1018", "CRS 1018"],
    "MAT-S235": ["Steel S235", "S235JR", "S235", "Mild steel"],
    "MAT-C360": ["Brass C360", "C360", "CuZn39Pb3", "Brass"],
    "MAT-POM": ["POM", "Delrin", "Acetal", "POM-C"],
    "MAT-TI64": ["Ti-6Al-4V", "Ti64", "Grade 5 Ti", "Titanium Gr5"],
}
LOST_REASONS = ["Price too high", "Lead time too long", "Went with a competitor",
                "Customer project cancelled", "We replied too late"]  # fmt: skip
OVERRIDE_NOTES = [
    ("Ramesh: long-standing customer, keep last year's price", 0.90),
    ("Ramesh: titanium/stainless always runs long, +15 %", 1.15),
    ("Ramesh: thin walls will chatter, add 10 %", 1.10),
    ("Ramesh: they'll order again, sharpen the pencil", 0.88),
    ("Ramesh: tight tolerance on a big part, scrap risk, +20 %", 1.20),
    ("Ramesh: round down, competitor quoted them low last time", 0.92),
]
# W14: systematic estimate errors per family (what S18 should discover).
FAMILY_TIME_BIAS = {"flanged_housing": 1.22, "spur_gear": 1.12, "sheet_panel": 0.93, "spacer": 0.95,
                    "l_bracket": 1.03, "u_bracket": 1.05, "keyed_shaft": 1.08, "mounting_plate": 1.0}  # fmt: skip


@dataclass
class Customer:
    key: str  # partner ref, e.g. CUST-007 (duplicates: CUST-007-DUP)
    name: str
    tier: str
    country: str
    language: str
    certs: str
    nda: bool
    email: str
    duplicate_of: str | None = None


@dataclass
class Supplier:
    key: str
    name: str
    kind: str
    email: str
    pricelist: list[tuple[str, float, int, date]]  # (product code, $/unit, lead days, valid from)


@dataclass
class PartRec:
    code: str  # product default_code, e.g. D-10023-A
    drawing: str
    rev: str
    family: str
    label: str
    spec: PartSpec
    params: dict[str, float]
    grade_text: str
    customer_key: str
    has_routing: bool
    stock_kg: float  # per part (BOM quantity)
    cycle_min: dict[str, float]  # BOM operation minutes per part


@dataclass
class Job:
    kind: str  # "done" | "open" | "outsourced"
    confirm_at: datetime
    commitment_at: datetime
    material_po: tuple[str, float, float, datetime, datetime] | None  # supplier, kg, $/kg, ordered, received
    subcon_po: tuple[str, float, datetime, datetime] | None  # supplier, unit $, ordered, received
    mo_start: datetime | None = None
    mo_finish: datetime | None = None
    delivered_at: datetime | None = None
    wo_actual_min: dict[str, float] = field(default_factory=dict)  # total minutes for the lot
    scrap_pct: float = 0.0


@dataclass
class Quote:
    ref: str  # client_order_ref, e.g. HIST-0001
    partner_key: str  # may be a duplicate record (W4)
    customer_key: str  # the real customer
    part_code: str
    qty: int
    received_at: datetime
    sent_at: datetime
    model_unit_price: float
    unit_price: float
    note: str | None
    outcome: str  # "won" | "lost" | "open"
    decided_at: datetime | None
    lost_reason: str | None = None
    tag: str = ""  # "W8" | "W11" | "repeat" | "revB" | ""
    job: Job | None = None


@dataclass
class BlanketMO:
    ref: str
    part_code: str
    customer_key: str
    qty: int
    start_at: datetime


@dataclass
class History:
    today: date
    customers: list[Customer]
    suppliers: list[Supplier]
    parts: dict[str, PartRec]
    quotes: list[Quote]
    blanket_mos: list[BlanketMO]
    stock_kg: dict[str, float]


# ---------------------------------------------------------------- material prices over time
def spike_date(today: date) -> date:
    """The metal price spike (about 21 months ago), behind the W8 losses."""
    return today - timedelta(days=630)


def price_index(material: str, d: date, today: date) -> float:
    """Synthetic $/kg index (1.0 = today's Odoo price). Metals: slow rise, +30 % spike, easing."""
    start, spike = today - timedelta(days=365 * YEARS), spike_date(today)
    if material == "MAT-POM":
        return 0.95 + 0.05 * max(0.0, min(1.0, (d - start).days / (today - start).days))
    if d < spike:
        return 0.78 + 0.07 * max(0.0, (d - start).days / (spike - start).days)
    return 1.12 - 0.12 * min(1.0, (d - spike).days / (today - spike).days)


def price_on(material: str, d: date, today: date) -> float:
    return round(PRICE_PER_KG[material] * price_index(material, d, today), 2)


# ---------------------------------------------------------------- helpers
def at(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute)


def workday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def odoo_cycle(p: PartSpec, b: CostBreakdown) -> dict[str, float]:
    """BOM operation minutes per part. CMM: average sampled inspection per part."""
    cyc = {wc: round(m, 2) for wc, m in b.run_min.items()}
    if "CMM" in cyc:
        cyc["CMM"] = round((p.holes + p.threads + p.pockets + 4) * 0.5 * 0.1, 2)
    return cyc


def lot_minutes(part: PartRec, qty: int, wc: str) -> float:
    """What Odoo computes for a work order: setup + cleanup + qty x cycle (efficiency 100 %)."""
    from legacy.odoo.setup_odoo import WORKCENTERS

    stop = {w[1]: float(w[4]) for w in WORKCENTERS}
    return SETUP_MIN[wc] + stop[wc] + qty * part.cycle_min[wc]


def actual_cost(part: PartRec, qty: int, job: Job, today: date) -> float:
    """What the job really cost (S18's view): material bought + actual machine time + outside + overhead."""
    b = estimate(part.spec, qty, "B")
    if job.material_po:
        material = job.material_po[1] * job.material_po[2]
    else:
        material = b.material * qty
    machine = sum(m / 60.0 * RATE[wc] for wc, m in job.wo_actual_min.items())
    inspection = b.inspection * qty
    return (material + machine + inspection) * (1 + OVERHEAD) + b.outside * qty


# ---------------------------------------------------------------- generation
def generate(today: date | None = None) -> History:
    today = today or date.today()
    rng = random.Random(SEED)
    start = today - timedelta(days=365 * YEARS)

    customers = _customers(rng)
    suppliers = _suppliers(rng, today)
    real = [c for c in customers if c.duplicate_of is None]
    dup_of = {c.duplicate_of: c.key for c in customers if c.duplicate_of}
    weights = [{"A": 3.0, "B": 1.5, "C": 1.0}[c.tier] for c in real]

    parts: dict[str, PartRec] = {}
    quotes: list[Quote] = []
    next_drawing = [10001]

    def new_part(customer: Customer, base: Part | None = None, rev: str = "A") -> PartRec:
        part = base or FAMILIES[rng.choices(list(FAMILIES), FAMILY_WEIGHTS)[0]](rng)
        drawing = f"D-{next_drawing[0]}"
        next_drawing[0] += 1
        return _register(parts, part, drawing, rev, customer.key, rng)

    def add_quote(cust: Customer, part: PartRec, qty: int, received: date, tag: str = "",
                  tier_markup_tier: str | None = None) -> Quote:  # fmt: skip
        delay = rng.choices([1, 2, 3, 4, 5, 6, 7, 8], [6, 10, 18, 22, 20, 12, 8, 4])[0]  # W1: 3-5 days
        sent_d = workday(received + timedelta(days=delay))
        b = estimate(part.spec, qty, tier_markup_tier or cust.tier, price_on(part.spec.material, sent_d, today))
        noisy = b.unit_price * math.exp(rng.gauss(0, 0.10))
        note = None
        if rng.random() < 0.07:
            note, mult = rng.choice(OVERRIDE_NOTES)
            noisy *= mult
        partner = dup_of[cust.key] if cust.key in dup_of and rng.random() < 0.3 else cust.key
        q = Quote(f"HIST-{len(quotes) + 1:04d}", partner, cust.key, part.code, qty,
                  at(received, rng.randint(7, 16), rng.choice([0, 15, 30, 45])),
                  at(sent_d, rng.randint(9, 17), rng.choice([0, 10, 20, 40])),
                  b.unit_price, round(noisy, 2), note, "lost", None, tag=tag)  # fmt: skip
        quotes.append(q)
        return q

    # 1) the regular RFQ stream
    days = sorted(start + timedelta(days=rng.randint(0, (today - start).days - 7)) for _ in range(N_QUOTES - 14))
    quoted_parts: list[tuple[Customer, PartRec]] = []
    revb_done: set[str] = set()
    for d in days:
        d = workday(d) if workday(d) < today else d
        cust = rng.choices(real, weights)[0]
        own = [p for c, p in quoted_parts if c.key == cust.key]
        r = rng.random()
        if own and r < 0.13:  # W10: repeat part, quoted from scratch again
            part = rng.choice(own)
            add_quote(cust, part, rng.choice(QTY_CHOICES[part.family]), d, "repeat")
        elif own and r < 0.25 and len(revb_done) < 42:
            cands = [p for p in own if p.rev == "A" and p.drawing not in revb_done]
            if cands:
                a = rng.choice(cands)
                revb_done.add(a.drawing)
                part = _revision(parts, a, rng)
                add_quote(cust, part, rng.choice(QTY_CHOICES[part.family]), d, "revB")
                quoted_parts.append((cust, part))
                continue
            part = new_part(cust)
            add_quote(cust, part, rng.choice(QTY_CHOICES[part.family]), d)
            quoted_parts.append((cust, part))
        else:
            part = new_part(cust)
            add_quote(cust, part, rng.choice(QTY_CHOICES[part.family]), d)
            quoted_parts.append((cust, part))

    # 2) W8: tier-A, material-heavy quotes just before the spike, accepted months later
    spike = spike_date(today)
    tier_a = [c for c in real if c.tier == "A"]
    w8 = 0
    while w8 < 10:
        cust = rng.choice(tier_a)
        fam = rng.choice(["mounting_plate", "flanged_housing", "u_bracket"])
        base = FAMILIES[fam](rng)
        base.spec.material = rng.choice(["MAT-SS316", "MAT-AL7075", "MAT-SS304"])
        part = new_part(cust, base)
        received = workday(spike - timedelta(days=rng.randint(30, 150)))
        q = add_quote(cust, part, rng.choice([25, 50, 100]), received, "W8")
        q.note = None
        q.unit_price = round(q.model_unit_price * rng.uniform(0.97, 1.0), 2)
        q.outcome, q.decided_at = "won", q.sent_at + timedelta(days=rng.randint(120, 240))
        q.job = _job(q, part, today, rng, suppliers)
        if actual_cost(part, q.qty, q.job, today) > q.unit_price * q.qty:
            w8 += 1
        else:  # not a loss: drop it and try again (we want exactly the W8 story)
            quotes.pop()
            del parts[part.code]

    # 3) W11: out-of-capability parts, won, then outsourced at a loss
    for i in range(4):
        cust = rng.choice(real)
        base = FAMILIES["mounting_plate"](rng) if i % 2 == 0 else FAMILIES["flanged_housing"](rng)
        if i % 2 == 0:  # too big for any mill (max X 762 mm)
            base.spec.length = float(rng.randint(850, 1100))
        else:  # tighter than the best machine can hold (0.005 mm)
            base.spec.tolerance_mm = 0.002
        part = new_part(cust, base)
        received = workday(start + timedelta(days=rng.randint(60, 365 * YEARS - 90)))
        q = add_quote(cust, part, rng.choice([2, 5, 10]), received, "W11", tier_markup_tier="B")
        q.note = None
        q.outcome, q.decided_at = "won", q.sent_at + timedelta(days=rng.randint(5, 20))

    quotes.sort(key=lambda q: q.received_at)
    for i, q in enumerate(quotes, 1):
        q.ref = f"HIST-{i:04d}"

    _outcomes(quotes, parts, real, today, rng)
    for q in quotes:
        if q.outcome == "won" and q.job is None:
            q.job = _job(q, parts[q.part_code], today, rng, suppliers)

    blanket = _blanket(quotes, parts, today, rng)
    stock = {m: float(rng.randint(80, 600)) for m in PRICE_PER_KG}
    return History(today, customers, suppliers, parts, quotes, blanket, stock)


def slug(name: str) -> str:
    """Mailbox name: ASCII letters and digits only ("Krämer" -> "kramer"), at most 20 characters."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return "".join(ch for ch in ascii_name.lower() if ch.isalnum())[:20]


def _customers(rng: random.Random) -> list[Customer]:
    out = []
    tiers = ["A"] * 6 + ["B"] * 14 + ["C"] * 15
    rng.shuffle(tiers)
    for i, name in enumerate(CUSTOMER_NAMES, 1):
        country, lang = CUSTOMER_COUNTRY.get(name, ("US", "English"))
        c = Customer(f"CUST-{i:03d}", name, tiers[i - 1], country, lang, rng.choice(CERTS),
                     rng.random() < 0.2, f"{slug(name)}@customers.local")  # fmt: skip
        out.append(c)
        if name in DUPLICATES:
            out.append(Customer(f"{c.key}-DUP", DUPLICATES[name], c.tier, country, lang, "", False,
                                c.email if rng.random() < 0.5 else f"purchasing.{c.email}",
                                duplicate_of=c.key))  # fmt: skip
    return out


def _suppliers(rng: random.Random, today: date) -> list[Supplier]:
    from legacy.seed.costing_model import OSP_PRICE

    out = []
    for i, (name, kind, codes) in enumerate(SUPPLIERS, 1):
        pl = []
        for code in codes:
            valid_from = today - timedelta(days=rng.randint(20, 720))  # W13: many > 6 months old
            if code.startswith("MAT-"):
                price, lead = price_on(code, valid_from, today) * rng.uniform(0.95, 1.08), rng.randint(5, 21)
            else:
                price, lead = OSP_PRICE[code] * rng.uniform(0.9, 1.15), rng.randint(5, 12)
            pl.append((code, round(price, 2), lead, valid_from))
        out.append(Supplier(f"SUP-{i:02d}", name, kind, f"{slug(name)}@suppliers.local", pl))
    return out


def _register(parts: dict[str, PartRec], part: Part, drawing: str, rev: str, cust_key: str,
              rng: random.Random) -> PartRec:  # fmt: skip
    b = estimate(part.spec, 10, "B")
    grade = part.spec.material
    text = GRADE_VARIANTS[grade][0] if rng.random() < 0.55 else rng.choice(GRADE_VARIANTS[grade][1:])
    rec = PartRec(f"{drawing}-{rev}", drawing, rev, part.family, part.label, part.spec, part.params, text,
                  cust_key, True, b.stock_kg, odoo_cycle(part.spec, b))  # fmt: skip
    parts[rec.code] = rec
    return rec


def _revision(parts: dict[str, PartRec], a: PartRec, rng: random.Random, rev: str = "B") -> PartRec:
    import copy

    spec = copy.deepcopy(a.spec)
    change = rng.choice(["holes", "tolerance", "length"])
    if change == "holes":
        spec.holes += 2
    elif change == "tolerance":
        spec.tolerance_mm = {0.1: 0.05, 0.05: 0.02, 0.02: 0.01}.get(spec.tolerance_mm, spec.tolerance_mm)
        if spec.tolerance_mm <= 0.02 and "CMM" not in spec.routing:
            spec.routing.append("CMM")
    else:
        spec.length = round(spec.length * 1.1)
        spec.finished_volume_cm3 *= 1.1
    part = Part(a.family, a.label, spec, dict(a.params, rev_change=0.0))
    return _register(parts, part, a.drawing, rev, a.customer_key, rng)


def _outcomes(quotes: list[Quote], parts: dict[str, PartRec], real: list[Customer], today: date,
              rng: random.Random) -> None:  # fmt: skip
    tier = {c.key: c.tier for c in real}
    free = [q for q in quotes if q.tag not in ("W8", "W11")]
    raw = []
    for q in free:
        delay = (q.sent_at.date() - q.received_at.date()).days
        p = math.exp(-0.15 * (delay - 1)) * (q.model_unit_price / q.unit_price) ** 4
        p *= {"A": 1.3, "B": 1.0, "C": 0.8}[tier[q.customer_key]]
        raw.append(p)
    # Calibrate so the expected win rate over decided quotes is ~27 % (including the forced wins).
    target_wins = 0.27 * len(quotes) - 14
    scale = target_wins / sum(raw)
    for q, p in zip(free, raw, strict=True):
        won = rng.random() < min(0.9, p * scale)
        lag = rng.randint(3, 25) if won else rng.randint(5, 30)
        decided = q.sent_at + timedelta(days=lag)
        q.outcome, q.decided_at = ("won" if won else "lost"), decided
        if decided.date() >= today:
            q.outcome, q.decided_at = "open", None
    lost = [q for q in quotes if q.outcome == "lost"]
    for q in rng.sample(lost, round(0.2 * len(lost))):  # W9: reason recorded on only ~20 %
        q.lost_reason = rng.choice(LOST_REASONS)
    # W4: ~15 % of parts have no routing (old products entered without operations).
    special = {q.part_code for q in quotes if q.tag in ("W8", "W11")}
    codes = sorted(c for c in parts if c not in special)
    for code in rng.sample(codes, round(0.15 * len(parts))):
        parts[code].has_routing = False


def _job(q: Quote, part: PartRec, today: date, rng: random.Random, suppliers: list[Supplier]) -> Job:
    assert q.decided_at is not None
    confirm = q.decided_at
    commitment = confirm + timedelta(days=PROMISE_DAYS)
    if q.tag == "W11":
        unit = q.unit_price * rng.uniform(1.15, 1.4)  # the subcontractor costs more than we sold for
        ordered = confirm + timedelta(days=1)
        received = confirm + timedelta(days=rng.randint(24, 45))
        return Job("outsourced", confirm, commitment, None, (SUBCONTRACTOR, round(unit, 2), ordered, received),
                   delivered_at=received + timedelta(days=1))  # fmt: skip
    # Material is bought per job at the price of the day.
    sups = [s for s in suppliers if any(c == part.spec.material for c, *_ in s.pricelist)]
    sup = rng.choice(sups)
    scrap = rng.uniform(0.0, 0.12) + (0.05 if part.spec.material == "MAT-TI64" else 0.0)
    kg = round(part.stock_kg * q.qty * (1 + scrap), 3)
    ordered = confirm + timedelta(hours=rng.randint(2, 30))
    received = ordered + timedelta(days=rng.randint(2, 5))
    ppk = round(price_on(part.spec.material, ordered.date(), today) * rng.uniform(0.97, 1.04), 2)
    # W7: ~70 % on time against a fixed 3-week promise.
    late = rng.random() < 0.30
    lead = rng.randint(PROMISE_DAYS + 1, PROMISE_DAYS + 24) if late else rng.randint(9, PROMISE_DAYS)
    delivered = confirm + timedelta(days=lead)
    mo_start = received + timedelta(hours=rng.randint(4, 30))
    mo_finish = max(delivered - timedelta(hours=rng.randint(4, 20)), mo_start + timedelta(hours=8))
    # W14: actual minutes differ from the estimate (family bias + noise).
    bias = FAMILY_TIME_BIAS[part.family]
    actual = {}
    if part.has_routing:
        for wc in part.cycle_min:
            actual[wc] = round(lot_minutes(part, q.qty, wc) * bias * math.exp(rng.gauss(0, 0.15)), 1)
    kind = "done" if delivered.date() < today else "open"
    job = Job(kind, confirm, commitment, (sup.key, kg, ppk, ordered, received), None, mo_start, mo_finish,
              delivered if kind == "done" else None, actual, round(scrap, 4))  # fmt: skip
    return job


def _blanket(quotes: list[Quote], parts: dict[str, PartRec], today: date, rng: random.Random) -> list[BlanketMO]:
    """Repeat production for existing customers, filling the main machines to 60-90 % for 6 weeks."""
    horizon = today + timedelta(days=42)
    load = dict.fromkeys(LOAD_TARGET_WCS, 0.0)
    for q in quotes:
        if q.job and q.job.kind == "open" and q.job.mo_start and q.job.mo_start.date() < horizon:
            part = parts[q.part_code]
            if part.has_routing:
                for wc in load:
                    if wc in part.cycle_min:
                        load[wc] += lot_minutes(part, q.qty, wc)
    target = {wc: CAPACITY_MIN_6W * rng.uniform(0.65, 0.85) for wc in LOAD_TARGET_WCS}
    cap = CAPACITY_MIN_6W * 0.88  # 90 % is the limit; Odoo rounds each work order's duration up, so leave headroom
    won_parts = sorted({q.part_code for q in quotes if q.outcome == "won" and q.tag != "W11"})
    cands = [parts[c] for c in won_parts if parts[c].has_routing]
    out: list[BlanketMO] = []
    for _ in range(400):
        short = [wc for wc in LOAD_TARGET_WCS if load[wc] < target[wc]]
        if not short:
            break
        wc = max(short, key=lambda w: target[w] - load[w])
        pool = [p for p in cands if wc in p.cycle_min and p.cycle_min[wc] > 0]
        part = rng.choice(pool)
        per_part = part.cycle_min[wc]
        qty = int(min((target[wc] - load[wc]) / per_part, 4000 / per_part, 5000))
        # never push another target machine over 90 %
        for other in LOAD_TARGET_WCS:
            if other != wc and other in part.cycle_min and part.cycle_min[other] > 0:
                room = cap - load[other] - SETUP_MIN[other]
                qty = min(qty, int(room / part.cycle_min[other]))
        if qty < 5:
            target[wc] = load[wc]  # can't fill further without overloading a neighbour
            continue
        for w in LOAD_TARGET_WCS:
            if w in part.cycle_min:
                load[w] += lot_minutes(part, qty, w)
        start_d = workday(today + timedelta(days=rng.randint(0, 35)))
        out.append(BlanketMO(f"BLK-{len(out) + 1:03d}", part.code, part.customer_key, qty,
                             at(start_d, 6)))  # fmt: skip
    return out


def machine_load(h: History) -> dict[str, float]:
    """Planned load (fraction of 6-week capacity) per target work center, as generated."""
    horizon = h.today + timedelta(days=42)
    load = dict.fromkeys(LOAD_TARGET_WCS, 0.0)
    rows = [(q.part_code, q.qty, q.job.mo_start) for q in h.quotes if q.job and q.job.kind == "open"]
    rows += [(b.part_code, b.qty, b.start_at) for b in h.blanket_mos]
    for code, qty, start_at in rows:
        part = h.parts[code]
        if part.has_routing and start_at and start_at.date() < horizon:
            for wc in load:
                if wc in part.cycle_min:
                    load[wc] += lot_minutes(part, qty, wc)
    return {wc: v / CAPACITY_MIN_6W for wc, v in load.items()}


if __name__ == "__main__":
    h = generate()
    won = [q for q in h.quotes if q.outcome == "won"]
    lost = [q for q in h.quotes if q.outcome == "lost"]
    done = [q for q in won if q.job and q.job.kind == "done"]
    on_time = [q for q in done if q.job and q.job.delivered_at and q.job.delivered_at <= q.job.commitment_at]
    print(f"customers {len(h.customers)}  suppliers {len(h.suppliers)}  parts {len(h.parts)}  quotes {len(h.quotes)}")
    print(f"won {len(won)}  lost {len(lost)}  open {len(h.quotes) - len(won) - len(lost)}  "
          f"win rate {len(won) / (len(won) + len(lost)):.1%}")  # fmt: skip
    print(f"lost with reason {sum(1 for q in lost if q.lost_reason) / len(lost):.0%}  "
          f"on-time {len(on_time) / len(done):.0%} of {len(done)} delivered")  # fmt: skip
    print("no routing", f"{sum(1 for p in h.parts.values() if not p.has_routing) / len(h.parts):.0%}",
          " rev pairs", sum(1 for p in h.parts.values() if p.rev == "B"),
          " repeats", sum(1 for q in h.quotes if q.tag == "repeat"))  # fmt: skip
    print("blanket MOs", len(h.blanket_mos), " load", {k: f"{v:.0%}" for k, v in machine_load(h).items()})
