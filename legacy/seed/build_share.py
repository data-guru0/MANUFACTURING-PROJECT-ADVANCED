"""L4: fill \\\\fileserver\\Quotes the way 10 years of estimators did. Every file is written THROUGH SMB
with the one shared login (W16).

What lands on the share:
- Pricing_Calculator_v7_FINAL(2).xlsx: the legacy formula (costing_model.py) as LIVE Excel formulas,
  with lookup sheets (Materials, Rates, Factors) and a "Ramesh notes" sheet of free-text rules (W6).
  Its Materials sheet carries the STALE 2023 prices (W5).
- Material_Prices_2023.xlsx (stale, messy names: W5), Machine_Rates.xlsx, a README nobody reads.
- Customers/<name>/Quote_<date>.xlsx: ~150 filled-in copies of the calculator. ~120 match quotes in Odoo,
  ~30 exist ONLY here (W5). Folder names follow the (duplicate) Odoo partners (W4).

Run: uv run python -m legacy.seed.build_share   (needs the fileserver container)
"""

import io
import logging
import random
import time
from datetime import date, datetime, timedelta
from typing import Any

import openpyxl
import smbclient
from openpyxl.styles import Font, PatternFill
from openpyxl.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from legacy.odoo.rpc import env
from legacy.odoo.setup_odoo import MATERIALS, OUTSIDE_PROCESSES, WORKCENTERS
from legacy.seed import costing_model as cm
from legacy.seed.history import YEARS, History, PartRec, generate, price_on
from legacy.seed.part_families import FAMILIES, FAMILY_WEIGHTS, QTY_CHOICES

SERVER, PORT, SHARE = "127.0.0.1", 1445, "Quotes"
logging.getLogger("smbprotocol").setLevel(logging.ERROR)  # quiet "socket aborted" on close
ROOT = rf"\\{SERVER}\{SHARE}"
CALC_NAME = "Pricing_Calculator_v7_FINAL(2).xlsx"
SEED = 42_2023
WC_ORDER = ["MILL3", "MILL5", "LATHE", "LASER", "BRAKE", "SAW", "DEBURR", "CMM"]
OSP_ORDER = [p[0] for p in OUTSIDE_PROCESSES]
STALE_NAMES = {"MAT-AL6061": "Alu 6061 T6", "MAT-AL7075": "Alu 7075", "MAT-SS304": "SS 304", "MAT-SS316": "SS316L",
               "MAT-1018": "CRS 1018", "MAT-S235": "Mild steel S235", "MAT-C360": "Brass 360", "MAT-POM": "Delrin",
               "MAT-TI64": "Titanium Gr5"}  # fmt: skip
RAMESH_NOTES = [
    ("2019-03", "Titanium and stainless ALWAYS run long. Add 15 % on top of whatever the sheet says."),
    ("2019-11", "Thin walls under 2 mm chatter on the VF-2. Add 10 %, or put it on the 5-axis."),
    ("2020-06", "Repeat customers (tier A): never more than last price + 5 %. Check the old quote first!"),
    ("2021-02", "Brightline raised the anodize lot charge to $175. Sheet still says 150, fix it someday."),
    ("2021-09", "Kestrel pays 60 days late every time. Add 2 % or ask Meera."),
    ("2022-01", "Tolerance 0.005 or tighter: we scrap 1 in 10 on the 5-axis. Add 20 % and FAI."),
    ("2022-07", "Rush (< 2 weeks): +25 %, and tell Kavita BEFORE you promise anything."),
    ("2022-10", "Sheet panels over 1 m: the brake needs two people. Add 30 min per bend batch."),
    ("2023-03", "Brass is cheap to cut but the chips are worth money. Don't give it away."),
    ("2023-08", "If the customer sends a STEP file, trust it over the PDF. Inch drawings: double-check units!"),
    ("2024-05", "Competitor in Pune quotes spacers at cost. Sharpen the pencil on qty >= 1000."),
    ("2025-04", "Metal went up ~30 % this spring. Old quotes still out there at old prices... risky."),
]

INPUT_ROWS = {"customer": 2, "part": 3, "date": 4, "material": 5, "process": 6, "length": 7, "width": 8, "height": 9,
              "finished_vol": 10, "holes": 11, "threads": 12, "pockets": 13, "bends": 14, "cut_length": 15,
              "setups": 16, "tolerance": 17, "ra": 18, "qty": 19, "tier": 20}  # fmt: skip
WC_ROW0, OSP_ROW0 = 23, 33  # first routing row, first outside-process row
QUOTE_ROW = 40  # quoted price, note, Odoo quote number
UNIT_PRICE_CELL = "I25"


def stale_price(code: str, today: date) -> float:
    """The 2023 price list: what the metal cost when the sheet was last updated (~3 years ago)."""
    return price_on(code, today - timedelta(days=365 * YEARS - 30), today)


# ------------------------------------------------------------------ workbook
def lookup_sheets(wb: Workbook, today: date) -> None:
    mats = wb.create_sheet("Materials")
    mats.append(["Grade", "Code", "Density g/cm3", "Price $/kg (2023)", "MRR cm3/min", "Laser cut factor"])
    for code, _name, grade, density, _price in MATERIALS:
        mats.append([grade, code, density, stale_price(code, today), cm.MRR[code], cm.CUT_FACTOR.get(code, 0.8)])
    rates = wb.create_sheet("Rates")
    rates.append(["Code", "Work center", "$/h", "Setup min"])
    for _name, code, rate, setup, *_ in WORKCENTERS:
        rates.append([code, _name, rate, setup])
    f = wb.create_sheet("Factors")
    f["A1"], f["B1"], f["D1"], f["E1"] = "Tol >= mm", "Factor", "Ra >= um", "Factor"
    for i, (t, k) in enumerate(sorted(cm.TOL_FACTOR), 2):
        f[f"A{i}"], f[f"B{i}"] = t, k
    for i, (t, k) in enumerate(sorted(cm.RA_FACTOR), 2):
        f[f"D{i}"], f[f"E{i}"] = t, k
    standard: list[tuple[str, str, list[float]]] = [
        ("G", "Plate mm", [float(x) for x in cm.PLATE_MM]),
        ("H", "Bar dia mm", [float(x) for x in cm.BAR_DIA_MM]),
        ("I", "Sheet mm", cm.SHEET_MM),
    ]
    for col, title, values in standard:
        f[f"{col}1"] = title
        for i, v in enumerate(values, 2):
            f[f"{col}{i}"] = v
    f["K1"], f["L1"] = "Tier", "Markup"
    for i, (tier_name, m) in enumerate(cm.TIER_MARKUP.items(), 2):
        f[f"K{i}"], f[f"L{i}"] = tier_name, m
    f["N1"], f["O1"], f["P1"] = "Outside process", "$/part", "Lot min $"
    for i, code in enumerate(OSP_ORDER, 2):
        f[f"N{i}"], f[f"O{i}"], f[f"P{i}"] = code, cm.OSP_PRICE[code], cm.OSP_LOT_MIN[code]
    f["R1"], f["S1"] = "Process", "Scrap factor"
    for i, (pr, s) in enumerate(cm.SCRAP.items(), 2):
        f[f"R{i}"], f[f"S{i}"] = pr, s
    f["U1"], f["V1"] = "Overhead", cm.OVERHEAD
    f["U2"], f["V2"] = "Min markup", cm.MIN_MARKUP


def round_up(x: str, rng: str) -> str:
    """Excel: smallest standard size >= x from a sorted list, else CEILING to 5 mm (= costing_model.round_up)."""
    m = f"MATCH({x},{rng},1)"
    return f"IF({x}>MAX({rng}),CEILING({x},5),IFERROR(INDEX({rng},{m}+(INDEX({rng},{m})<{x})),INDEX({rng},1)))"


def calculator_sheet(ws: Worksheet) -> None:
    bold = Font(bold=True)
    yellow = PatternFill("solid", fgColor="FFF2CC")
    ws["A1"], ws["H1"] = "INPUTS (yellow)", "CALCULATION - do not touch"
    ws["A1"].font = ws["H1"].font = bold
    labels = {"customer": "Customer", "part": "Part / drawing", "date": "Date", "material": "Material grade",
              "process": "Process (mill/turn/sheet)", "length": "Length mm", "width": "Width / dia mm",
              "height": "Height / thk mm", "finished_vol": "Finished volume cm3", "holes": "Holes", "threads": "Threads",
              "pockets": "Pockets", "bends": "Bends", "cut_length": "Laser cut length mm", "setups": "Setups",
              "tolerance": "Tightest tol mm", "ra": "Ra um", "qty": "Quantity", "tier": "Customer tier (A/B/C)"}  # fmt: skip
    for key, row in INPUT_ROWS.items():
        ws[f"A{row}"] = labels[key]
        ws[f"B{row}"].fill = yellow
    B = {k: f"$B${r}" for k, r in INPUT_ROWS.items()}
    mat = f"{B['material']},Materials!$A$2:$F$10"
    calc = {
        2: ("Density", f"=VLOOKUP({mat},3,FALSE)"),
        3: ("Price $/kg", f"=VLOOKUP({mat},4,FALSE)"),
        4: ("Removal rate", f"=VLOOKUP({mat},5,FALSE)"),
        5: ("Cut factor", f"=VLOOKUP({mat},6,FALSE)"),
        7: (
            "Stock L",
            f'=IF({B["process"]}="turn",CEILING({B["length"]}+5,5),IF({B["process"]}="sheet",'
            f"CEILING({B['length']}+20,5),CEILING({B['length']}+6,5)))",
        ),
        8: (
            "Stock W/dia",
            f'=IF({B["process"]}="turn",{round_up(B["width"] + "+3", "Factors!$H$2:$H$17")},'
            f'IF({B["process"]}="sheet",CEILING({B["width"]}+20,5),CEILING({B["width"]}+6,5)))',
        ),
        9: (
            "Stock H/thk",
            f'=IF({B["process"]}="turn",I8,IF({B["process"]}="sheet",'
            f"{round_up(B['height'], 'Factors!$I$2:$I$8')},{round_up(B['height'] + '+6', 'Factors!$G$2:$G$18')}))",
        ),
        10: ("Stock volume cm3", f'=IF({B["process"]}="turn",PI()/4*I8^2*I7/1000,I7*I8*I9/1000)'),
        11: ("Stock kg", "=I10*I2/1000"),
        12: ("Scrap factor", f"=VLOOKUP({B['process']},Factors!$R$2:$S$4,2,FALSE)"),
        13: ("MATERIAL $/part", "=I11*I3*I12"),
        14: ("Tolerance factor", f"=VLOOKUP({B['tolerance']}+1E-9,Factors!$A$2:$B$7,2,TRUE)"),
        15: ("Finish factor", f"=VLOOKUP({B['ra']}+1E-9,Factors!$D$2:$E$6,2,TRUE)"),
        16: ("Main machine", '=IF($B$25="Y","LATHE",IF($B$24="Y","MILL5",IF($B$23="Y","MILL3",IF($B$26="Y","LASER",""))))'),
        17: ("RUN $/part", f"=SUM(D{WC_ROW0}:D{WC_ROW0 + 7})"),
        18: ("SETUP $/part", f"=SUM(E{WC_ROW0}:E{WC_ROW0 + 7})/{B['qty']}"),
        19: (
            "INSPECTION $/part",
            f'=IF(AND($B${WC_ROW0 + 7}="Y",{B["tolerance"]}<=0.02),'
            f'({B["holes"]}+{B["threads"]}+{B["pockets"]}+4)*0.5/60*VLOOKUP("CMM",Rates!$A$2:$D$9,3,FALSE)'
            f"*(1/{B['qty']}+0.1),0)",
        ),
        20: ("OUTSIDE $/part", f"=SUM(C{OSP_ROW0}:C{OSP_ROW0 + 4})"),
        21: ("OVERHEAD $/part", "=Factors!$V$1*(I13+I18+I17+I19)"),
        22: ("UNIT COST", "=I13+I18+I17+I19+I20+I21"),
        23: (
            "Markup",
            f"=MAX(VLOOKUP({B['tier']},Factors!$K$2:$L$4,2,FALSE)-IF({B['qty']}>=1000,0.06,"
            f"IF({B['qty']}>=100,0.03,0)),Factors!$V$2)",
        ),
        25: ("UNIT PRICE", "=ROUND(I22*(1+I23),2)"),
        26: ("LOT TOTAL", f"=I25*{B['qty']}"),
    }
    for row, (label, formula) in calc.items():
        ws[f"H{row}"], ws[f"I{row}"] = label, formula
    for cell in ("H25", "I25", "H22", "I22"):
        ws[cell].font = bold

    # Routing: one row per work center. C = minutes per part, D = run $, E = setup $ per lot.
    ws[f"A{WC_ROW0 - 1}"], ws[f"B{WC_ROW0 - 1}"] = "ROUTING (Y)", ""
    ws[f"C{WC_ROW0 - 1}"], ws[f"D{WC_ROW0 - 1}"], ws[f"E{WC_ROW0 - 1}"] = "min/part", "run $", "setup $/lot"
    base = (f"(MAX(I10-{B['finished_vol']},0)/I4+{B['holes']}*0.5+{B['threads']}*0.8+{B['pockets']}*3)"
            f"*I14*I15+{B['setups']}*2")  # fmt: skip
    secondary = f"({B['pockets']}*3+{B['holes']}*0.5+{B['threads']}*0.8+2)*I14*I15+2"
    for i, wc in enumerate(WC_ORDER):
        r = WC_ROW0 + i
        ws[f"A{r}"] = wc
        ws[f"B{r}"].fill = yellow
        is_main = f'AND($I$16=A{r},{B["process"]}<>"sheet")'
        minutes = {
            "MILL3": f"IF({is_main},{base},{secondary})",
            "MILL5": f"IF({is_main},{base},{secondary})",
            "LATHE": f"IF({is_main},{base},0)",
            "LASER": f"({B['cut_length']}/(6000/{B['height']}*I5)+({B['holes']}+1)*0.05)*I14",
            "BRAKE": f"{B['bends']}*0.5*I14",
            "SAW": "1.5",
            "DEBURR": f"1+0.1*({B['holes']}+{B['threads']})",
            "CMM": "0",
        }[wc]
        rate = f"VLOOKUP(A{r},Rates!$A$2:$D$9,3,FALSE)"
        setup_min = f"VLOOKUP(A{r},Rates!$A$2:$D$9,4,FALSE)"
        ws[f"C{r}"] = f'=IF(B{r}="Y",{minutes},0)'
        ws[f"D{r}"] = f"=C{r}/60*{rate}"
        ws[f"E{r}"] = f'=IF(B{r}="Y",{setup_min}/60*{rate}*IF(A{r}=$I$16,{B["setups"]},1),0)'
    ws[f"A{OSP_ROW0 - 1}"], ws[f"C{OSP_ROW0 - 1}"] = "OUTSIDE PROCESSES (Y)", "$/part"
    for i, code in enumerate(OSP_ORDER):
        r = OSP_ROW0 + i
        ws[f"A{r}"] = code
        ws[f"B{r}"].fill = yellow
        ws[f"C{r}"] = (f'=IF(B{r}="Y",MAX(VLOOKUP(A{r},Factors!$N$2:$P$6,2,FALSE)*{B["qty"]},'
                       f'VLOOKUP(A{r},Factors!$N$2:$P$6,3,FALSE))/{B["qty"]},0)')  # fmt: skip
    ws[f"A{QUOTE_ROW}"], ws[f"A{QUOTE_ROW + 1}"], ws[f"A{QUOTE_ROW + 2}"] = (
        "QUOTED UNIT PRICE (what we sent)",
        "Note",
        "Odoo quote no.",
    )
    for r in (QUOTE_ROW, QUOTE_ROW + 1, QUOTE_ROW + 2):
        ws[f"B{r}"].fill = yellow
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["H"].width = 20


def notes_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("Ramesh notes")
    ws.append(["When", "Rule of thumb (Ramesh)"])
    for when, note in RAMESH_NOTES:
        ws.append([when, note])
    ws.column_dimensions["B"].width = 100


def calculator(today: date) -> Workbook:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Calculator"
    wb.calculation.fullCalcOnLoad = True  # Excel recalculates every formula when the file opens
    calculator_sheet(ws)
    lookup_sheets(wb, today)
    notes_sheet(wb)
    return wb


def fill(ws: Worksheet, p: PartRec, customer: str, when: date, qty: int, tier: str, quoted: float, note: str,
         odoo_no: str) -> None:  # fmt: skip
    s = p.spec
    grade = next(m[2] for m in MATERIALS if m[0] == s.material)
    values: dict[str, Any] = {
        "customer": customer,
        "part": f"{p.drawing} Rev {p.rev}",
        "date": when,
        "material": grade,
        "process": s.process,
        "length": s.length,
        "width": s.width,
        "height": s.height,
        "finished_vol": round(s.finished_volume_cm3, 3),
        "holes": s.holes,
        "threads": s.threads,
        "pockets": s.pockets,
        "bends": s.bends,
        "cut_length": round(s.cut_length_mm, 2),
        "setups": s.setups,
        "tolerance": s.tolerance_mm,
        "ra": s.ra_um,
        "qty": qty,
        "tier": tier,
    }
    for k, v in values.items():
        ws[f"B{INPUT_ROWS[k]}"] = v
    for i, wc in enumerate(WC_ORDER):
        ws[f"B{WC_ROW0 + i}"] = "Y" if wc in s.routing else None
    for i, code in enumerate(OSP_ORDER):
        ws[f"B{OSP_ROW0 + i}"] = "Y" if code in s.outside else None
    ws[f"B{QUOTE_ROW}"], ws[f"B{QUOTE_ROW + 1}"], ws[f"B{QUOTE_ROW + 2}"] = quoted, note, odoo_no


def spec_from_sheet(ws: Worksheet) -> tuple[cm.PartSpec, int, str]:
    """Read the inputs back from a calculator (used by the recalculation test)."""
    v = {k: ws[f"B{r}"].value for k, r in INPUT_ROWS.items()}
    code = next(m[0] for m in MATERIALS if m[2] == v["material"])
    # The sheet only has Y flags; rebuild the routing in shop-floor order (saw -> lathe -> mill ...),
    # because the first of LATHE/MILL5/MILL3/LASER is the main machine (same rule as the sheet's I16).
    flags = {wc for i, wc in enumerate(WC_ORDER) if ws[f"B{WC_ROW0 + i}"].value == "Y"}
    routing = [wc for wc in ["SAW", "LATHE", "MILL5", "MILL3", "LASER", "BRAKE", "DEBURR", "CMM"] if wc in flags]
    outside = [c for i, c in enumerate(OSP_ORDER) if ws[f"B{OSP_ROW0 + i}"].value == "Y"]
    spec = cm.PartSpec(
        v["process"],
        code,
        float(v["length"]),
        float(v["width"]),
        float(v["height"]),
        float(v["finished_vol"]),
        int(v["holes"]),
        int(v["threads"]),
        int(v["pockets"]),
        int(v["bends"]),
        float(v["cut_length"]),
        int(v["setups"]),
        float(v["tolerance"]),
        float(v["ra"]),
        routing,
        outside,
    )
    return spec, int(v["qty"]), str(v["tier"])


def to_bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ the share
def put(rel: str, data: bytes, when: datetime | None = None) -> None:
    path = ROOT + "\\" + rel
    parent = path.rsplit("\\", 1)[0]
    smbclient.makedirs(parent, exist_ok=True, port=PORT)
    with smbclient.open_file(path, mode="wb", port=PORT) as f:
        f.write(data)
    if when:
        ts = when.timestamp()
        smbclient.utime(path, times=(ts, ts), port=PORT)


def main() -> None:
    t0 = time.monotonic()
    smbclient.register_session(SERVER, username="fileshare", password=env("FILESHARE_PASSWORD"), port=PORT)
    h: History = generate()
    today = h.today
    rng = random.Random(SEED)

    put(CALC_NAME, to_bytes(calculator(today)), datetime(2023, 9, 14, 17, 2))
    old = calculator(today)
    put("Pricing_Calculator_v7_FINAL.xlsx", to_bytes(old), datetime(2023, 6, 2, 11, 40))
    prices = openpyxl.Workbook()
    ws = prices.active
    assert ws is not None
    ws.title = "Prices"
    ws.append(["Material", "$/kg", "Valid from", "Comment"])
    for code, *_ in MATERIALS:
        ws.append([STALE_NAMES[code], stale_price(code, today), "01/2023", "Midwest / Coastal quote"])
    put("Material_Prices_2023.xlsx", to_bytes(prices), datetime(2023, 1, 16, 9, 5))
    rates = openpyxl.Workbook()
    ws = rates.active
    assert ws is not None
    ws.title = "Rates"
    ws.append(["Machine", "$/hour", "Setup (min)"])
    for name, _code, rate, setup, *_ in WORKCENTERS:
        ws.append([name, rate, setup])
    put("Machine_Rates.xlsx", to_bytes(rates), datetime(2022, 4, 4, 8, 30))
    readme = ("USE Pricing_Calculator_v7_FINAL(2).xlsx NOT the other one!!\r\n"
              "Save each quote as Customers\\<customer>\\Quote_<date>.xlsx\r\n"
              "Prices in the calc are from the 2023 list. Ask Tom if in doubt. - R.\r\n")  # fmt: skip
    put("README - DONT DELETE.txt", readme.encode(), datetime(2023, 9, 14, 17, 5))

    partner_name = {c.key: c.name for c in h.customers}
    tier = {c.key: c.tier for c in h.customers}
    # ~120 quotes that are also in Odoo (sale order names follow HIST order: HIST-0001 -> S00001).
    odoo_quotes = rng.sample(h.quotes, 120)
    written: set[str] = set()
    n_odoo = n_xls = 0
    for q in sorted(odoo_quotes, key=lambda q: q.sent_at):
        p = h.parts[q.part_code]
        wb = calculator(today)
        calc_ws = wb["Calculator"]
        note = (q.note or "") + ("" if q.outcome != "lost" else "  (lost)")
        fill(calc_ws, p, partner_name[q.partner_key], q.sent_at.date(), q.qty, tier[q.customer_key], q.unit_price,
             note.strip(), f"S{int(q.ref.split('-')[1]):05d}")  # fmt: skip
        rel = _unique(written, f"Customers\\{folder_name(partner_name[q.partner_key])}\\Quote_{q.sent_at:%Y-%m-%d}.xlsx")
        put(rel, to_bytes(wb), q.sent_at)
        n_odoo += 1
    # ~30 quotes that were only ever done in Excel (never typed into Odoo): W5.
    real = [c for c in h.customers if c.duplicate_of is None]
    start = today - timedelta(days=365 * YEARS)
    for i in range(30):
        c = rng.choice(real)
        fam = rng.choices(list(FAMILIES), FAMILY_WEIGHTS)[0]
        part = FAMILIES[fam](rng)
        rec = PartRec(f"D-{15001 + i}-A", f"D-{15001 + i}", "A", fam, part.label, part.spec, part.params,
                      part.spec.material, c.key, True, 0.0, {})  # fmt: skip
        when = start + timedelta(days=rng.randint(0, 365 * YEARS - 10))
        qty = rng.choice(QTY_CHOICES[fam])
        b = cm.estimate(part.spec, qty, c.tier, price_on(part.spec.material, when, today))
        wb = calculator(today)
        fill(wb["Calculator"], rec, c.name, when, qty, c.tier, round(b.unit_price * rng.uniform(0.92, 1.1), 2),
             rng.choice(["phone quote", "rush, sent from home", "", "", "verbal OK from Meera"]), "")  # fmt: skip
        rel = _unique(written, f"Customers\\{folder_name(c.name)}\\Quote_{when.isoformat()}.xlsx")
        put(rel, to_bytes(wb), datetime(when.year, when.month, when.day, rng.randint(8, 18), rng.randint(0, 59)))
        n_xls += 1
    # The quote names depend on the day the history is generated for, so a reset on another day would otherwise
    # leave the previous day's quotes next to today's. Remove generated quotes this run did not write.
    # Folders are matched by file ID, not name (robust to Samba's name mangling).
    kept: dict[str, set[str]] = {}
    for rel in written:
        folder, name = rel.rsplit("\\", 1)
        kept.setdefault(folder, set()).add(name)
    by_id = {smbclient.stat(f"{ROOT}\\{folder}", port=PORT).st_ino: names for folder, names in kept.items()}
    removed = 0
    for d in smbclient.scandir(ROOT + "\\Customers", port=PORT):
        if not d.is_dir():
            continue
        names = by_id.get(d.inode(), set())  # a customer folder this run didn't write to: all its quotes are stale
        for f in smbclient.scandir(d.path, port=PORT):
            if f.is_file() and f.name.startswith("Quote_") and f.name.endswith(".xlsx") and f.name not in names:
                smbclient.remove(f.path, port=PORT)
                removed += 1
        if not names and not smbclient.listdir(d.path, port=PORT):  # e.g. a folder an older build named differently
            smbclient.rmdir(d.path, port=PORT)
    print(f"share: {CALC_NAME} + 4 files, {n_odoo + n_xls} customer quotes ({n_odoo} also in Odoo, "
          f"{n_xls} Excel-only), {removed} stale quotes removed, in {time.monotonic() - t0:.0f}s")  # fmt: skip
    smbclient.reset_connection_cache()


def folder_name(name: str) -> str:
    """A customer folder as Windows names it: no trailing dot or space ("Kestrel Automation Inc." can't be opened)."""
    return name.rstrip(". ")


def _unique(written: set[str], rel: str) -> str:
    out, n = rel, 2
    while out in written:
        out = rel.replace(".xlsx", f"_{n}.xlsx")
        n += 1
    written.add(out)
    return out


if __name__ == "__main__":
    main()
