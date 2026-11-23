"""W1-W18 with evidence queried live from the running legacy system -> docs/legacy/PAIN_POINTS.md.

Every "Evidence" cell is computed now, from Odoo (JSON-RPC), the mailboxes (IMAP), the file share
(SMB) or a container; nothing is typed in by hand. "See it yourself" says where to look in a UI.

Usage: uv run python -m scripts.pain_points
"""

import io
import re
import statistics
import subprocess
from datetime import datetime, timedelta

import openpyxl
import smbclient

from legacy.mail.client import imap
from legacy.odoo.rpc import ROOT, Odoo, env
from legacy.seed.build_share import CALC_NAME, PORT, SERVER
from legacy.seed.build_share import ROOT as SHARE
from scripts.baseline_metrics import Baseline, measure, odt, pct

REPORT = ROOT / "docs" / "legacy" / "PAIN_POINTS.md"
COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(ROOT / "docker-compose.legacy.yml")]

TITLE = {
    "W1": "Manual quoting, inbox worked oldest-first, replies take days",
    "W2": "Quote data scattered across many places",
    "W3": "One shared Odoo login and one shared mailbox password; no audit of who did what",
    "W4": "Dirty master data: duplicate customers, inconsistent material names, missing routings",
    "W5": "Stale price list on the share; quotes that exist only in Excel",
    "W6": "Pricing rules only in Ramesh's head and a free-text notes sheet",
    "W7": 'Delivery dates by gut feel ("3 weeks"), no capacity check',
    "W8": "Validity date not enforced, no price protection: late acceptances lose money",
    "W9": "No follow-ups; lost reason rarely recorded",
    "W10": "Repeat parts and revisions re-quoted from scratch",
    "W11": "No capability check: jobs outsourced at a loss",
    "W12": "No design-for-manufacturing or cost-down advice",
    "W13": "Supplier prices and lead times stale, chased by hand",
    "W14": "Nobody compares quoted with actual cost",
    "W15": "Customers ask for CO₂ per part; Apex can't answer",
    "W16": "File share writable by everyone; no backups, no monitoring",
    "W17": "No metrics for management",
    "W18": "Customer drawings forwarded whole to suppliers",
}
SEE = {
    "W1": "Roundcube `rfq@` → Inbox (sorted by date, unanswered) and Archive/Sent",
    "W2": "Odoo Sales, the share's `Customers\\` folders, Roundcube Archive, Ramesh notes sheet",
    "W3": "Odoo Sales → Quotations → group by Salesperson; Roundcube login `rfq@`",
    "W4": "Odoo Contacts → search `Kestrel`; Inventory → Products → group by Material grade",
    "W5": "`Pricing_Calculator_v7_FINAL(2).xlsx` → Materials vs Odoo product cost",
    "W6": "Calculator → Ramesh notes sheet; a quote's chatter in Odoo",
    "W7": "Odoo Sales → Orders → list with Delivery Date / Effective Date",
    "W8": "Odoo Sales → Orders → Expiration vs Order Date; Roundcube Sent quotes",
    "W9": "Odoo Sales → Quotations → Cancelled filter, group by Lost reason",
    "W10": "Odoo Inventory → Products → search a drawing number; Sales lines by product",
    "W11": "Odoo Purchase → vendor Precision Partners Machining",
    "W12": "Any Odoo quote or quote email: no design advice anywhere",
    "W13": "Odoo Purchase → Configuration → Vendor Pricelists (admin), sort by Start Date",
    "W14": "`BASELINE_METRICS.md` worst-jobs table (no Odoo screen shows this)",
    "W15": "Roundcube `rfq@` → search `CO2`",
    "W16": "Docker Desktop → apex-fileserver-1 → Exec: `testparm -s`",
    "W17": "Odoo has the fields but no screen shows them; `BASELINE_METRICS.md` is the first report",
    "W18": "Roundcube `purchasing@` → Sent → `FW: RFQ …` and its PDF title block",
}
FIXED_IN = {"W1": "S3–S8", "W2": "S2", "W3": "S2, S7, S9", "W4": "S2", "W5": "S2, S5", "W6": "S2, S18", "W7": "S5, S14",
            "W8": "S5, S15", "W9": "S15", "W10": "S12", "W11": "S11", "W12": "S13", "W13": "S17", "W14": "S18",
            "W15": "S5", "W16": "S9, S19", "W17": "S10, S15", "W18": "S9, S17"}  # fmt: skip


def imap_count(mailbox: str, folder: str, *criteria: str) -> int:
    m = imap(mailbox)
    try:
        m.select(folder, readonly=True)
        _, data = m.search(None, *criteria)
        return len(data[0].split())
    finally:
        m.logout()


def share_facts() -> tuple[float, int, int, int]:
    """(calculator's Al 6061 $/kg, Ramesh notes, quote files, Excel-only quotes) read over SMB."""
    smbclient.register_session(SERVER, username="fileshare", password=env("FILESHARE_PASSWORD"), port=PORT)

    def book(rel: str) -> openpyxl.Workbook:
        with smbclient.open_file(SHARE + "\\" + rel, mode="rb", port=PORT) as f:
            return openpyxl.load_workbook(io.BytesIO(f.read()))

    calc = book(CALC_NAME)
    al = next(float(str(r[3].value)) for r in calc["Materials"].iter_rows(min_row=2) if r[1].value == "MAT-AL6061")
    notes = sum(1 for r in calc["Ramesh notes"].iter_rows(min_row=2) if r[1].value)
    files = [f"Customers\\{c}\\{f}" for c in smbclient.listdir(SHARE + r"\Customers", port=PORT)
             for f in smbclient.listdir(SHARE + rf"\Customers\{c}", port=PORT)]  # fmt: skip
    excel_only = sum(1 for f in files if not book(f)["Calculator"]["B42"].value)
    return al, notes, len(files), excel_only


def evidence(o: Odoo, b: Baseline) -> dict[str, str]:
    sos = b.sos
    won = [s for s in sos if s["state"] == "sale"]
    more = {s["id"]: s for s in o.call("sale.order", "search_read", [("client_order_ref", "=like", "HIST-%")],
                                       fields=["user_id", "date_order", "validity_date"])}  # fmt: skip
    users = {more[s["id"]]["user_id"][1] for s in sos}
    al_calc, notes, quote_files, excel_only = share_facts()
    al_odoo = o.call("product.product", "search_read", [("default_code", "=", "MAT-AL6061")], fields=["standard_price"])[0]
    chatter = o.call("mail.message", "search_count", [("model", "=", "sale.order"), ("body", "ilike", "Ramesh")])
    dups = o.call("res.partner", "search_read", [("ref", "=like", "CUST-%-DUP")], fields=["name", "ref"])
    orig = {p["ref"]: p["name"] for p in o.call("res.partner", "search_read", [("ref", "in", [d["ref"][:-4] for d in dups])],
                                                fields=["name", "ref"])}  # fmt: skip
    pairs = "; ".join(f"“{orig[d['ref'][:-4]]}” vs “{d['name']}”" for d in dups[:3])
    grades = {p["x_material_grade"] for p in o.call("product.template", "search_read", [
        ("bom_ids.bom_line_ids.product_id.default_code", "=", "MAT-AL6061")], fields=["x_material_grade"])}  # fmt: skip
    boms = o.call("mrp.bom", "search_read", [("product_tmpl_id.x_drawing_no", "!=", False)], fields=["operation_ids"])
    no_route = sum(1 for x in boms if not x["operation_ids"])
    promise = sum(1 for s in won if s["commitment_date"] and odt(s["commitment_date"]) - odt(more[s["id"]]["date_order"])
                  == timedelta(days=21))  # fmt: skip
    expired = [s for s in won if more[s["id"]]["validity_date"]
               and odt(more[s["id"]]["date_order"]).date() > datetime.strptime(more[s["id"]]["validity_date"], "%Y-%m-%d").date()]  # fmt: skip
    late = {s["name"] for s in won if odt(more[s["id"]]["date_order"]) - odt(s["x_quote_sent_at"]) > timedelta(days=90)}
    late_loss = [j for j in b.loss_jobs if j[0] in late]
    lines = o.call("sale.order.line", "search_read", [("order_id.client_order_ref", "=like", "HIST-%")], fields=["product_id"])
    per_part: dict[str, int] = {}
    for ln in lines:
        per_part[ln["product_id"][1]] = per_part.get(ln["product_id"][1], 0) + 1
    repeats = sum(1 for n in per_part.values() if n > 1)
    codes = [p["default_code"] for p in o.call("product.template", "search_read", [("x_drawing_no", "!=", False)],
                                               fields=["default_code"])]  # fmt: skip
    drawings: dict[str, int] = {}
    for c in codes:
        drawings[c.rsplit("-", 1)[0]] = drawings.get(c.rsplit("-", 1)[0], 0) + 1
    revised = sum(1 for n in drawings.values() if n > 1)
    by_name = {s["name"]: s for s in sos}
    subcon = o.call("purchase.order", "search_read", [("partner_id.ref", "=", "SUP-12")], fields=["origin", "amount_untaxed"])
    sub_loss = [p for p in subcon if p["origin"] in by_name and p["amount_untaxed"] > by_name[p["origin"]]["amount_untaxed"]]
    sinfo = o.call("product.supplierinfo", "search_read", [("date_start", "!=", False)], fields=["date_start"])
    stale = sum(1 for r in sinfo if datetime.strptime(r["date_start"], "%Y-%m-%d") < datetime.now() - timedelta(days=182))
    wos = o.call("mrp.workorder", "search_read", [("production_id.state", "=", "done")], fields=["duration", "duration_expected"])
    dev = statistics.mean(abs(w["duration"] / w["duration_expected"] - 1) for w in wos if w["duration_expected"])
    co2_fields = [k for k in o.call("product.template", "fields_get", attributes=["string"]) if "co2" in k.lower()]
    co2_mails = imap_count("rfq@apex.local", "INBOX", "TEXT", "CO2")
    design_mails = imap_count("rfq@apex.local", "Sent", "TEXT", "design")
    valid_mails = imap_count("rfq@apex.local", "Sent", "TEXT", "valid")
    testparm = subprocess.run([*COMPOSE, "exec", "-T", "fileserver", "testparm", "-s"], capture_output=True, text=True).stdout
    share_users = re.search(r"valid users = (\S+)", testparm)
    recycle = "recycle" in testparm
    views = o.call("ir.ui.view", "search_count", [("arch_db", "ilike", "x_rfq_received_at")])
    fw = imap_count("purchasing@apex.local", "Sent", "SUBJECT", '"FW: RFQ"')
    nda = o.call("res.partner", "search_count", [("x_nda_restricted", "=", True)])
    days = sorted(b.response_days.values())
    loss = b.loss_jobs
    return {
        "W1": f"{b.inbox[0]} RFQs waiting unanswered in the shared inbox; median reply **{statistics.median(days):.1f} days**, "
              f"p90 {statistics.quantiles(days, n=10)[-1]:.1f} days over {len(days)} quotes (IMAP).",
        "W2": f"One quote lives in ≥5 places: Odoo ({len(sos)} quotes), the share ({quote_files} Excel quotes, {excel_only} only there), "
              f"rfq@ Archive/Sent (IMAP), drawings attached to products, Ramesh's notes ({notes}) and chatter ({chatter}).",
        "W3": f"All {len(sos)} historical quotes have salesperson **{', '.join(sorted(users))}**; "
              f"all {imap_count('rfq@apex.local', 'Sent', 'ALL')} quote emails left from the one shared `rfq@` mailbox.",
        "W4": f"{len(dups)} duplicate customers ({pairs}); Al 6061 written {len(grades)} ways ({', '.join(sorted(grades)[:4])}…); "
              f"{no_route} of {len(boms)} BOMs ({pct(no_route, len(boms))}) have no routing.",
        "W5": f"Calculator: Al 6061 **${al_calc:.2f}/kg** vs Odoo **${al_odoo['standard_price']:.2f}/kg**; "
              f"{excel_only} of {quote_files} quote files on the share are not in Odoo.",
        "W6": f"{notes} free-text rules on the calculator's “Ramesh notes” sheet; {chatter} quotes carry a “Ramesh:” override note "
              "in their chatter, and nothing in Odoo applies them.",
        "W7": f"{promise} of {len(won)} won orders promised exactly 21 days after the order; on-time delivery "
              f"**{pct(b.on_time, b.delivered)}** ({b.on_time}/{b.delivered}).",
        "W8": f"{len(expired)} won orders were confirmed **after** their quote had expired, still at the quoted price; {len(late)} "
              f"were accepted >90 days later and {len(late_loss)} of those lost money; {valid_mails} of the quote emails state a validity.",
        "W9": f"Only **{pct(b.lost_with_reason, b.lost)}** of {b.lost} lost quotes have a lost reason; nothing tracks follow-ups.",
        "W10": f"{repeats} products were quoted more than once, each time from scratch; {revised} drawings exist in several revisions "
               "with no link between them.",
        "W11": f"{len(sub_loss)} of {len(subcon)} subcontracted jobs cost more to buy in than the customer paid "
               f"({', '.join(p['origin'] for p in sub_loss)}).",
        "W12": f"No DFM field or record in Odoo; {design_mails} of the quote emails in rfq@/Sent contain any design advice.",
        "W13": f"{stale} of {len(sinfo)} supplier price-list entries ({pct(stale, len(sinfo))}) are older than 6 months.",
        "W14": f"Actual work-order time differs from the estimate by **{100 * dev:.0f} %** on average; {len(loss)} of {len(b.jobs)} "
               f"finished jobs lost money (total ${sum(c - r for _, r, c in loss):,.0f}), and no screen shows it.",
        "W15": f"{co2_mails} RFQs in the inbox ask for a CO₂ footprint; Odoo has {len(co2_fields)} CO₂ fields to answer from.",
        "W16": f"Samba: `valid users = {share_users.group(1) if share_users else '?'}` (one shared account, read-write), "
               f"recycle bin {'on' if recycle else '**off**'}, no backup service in the stack: all {quote_files} customer quote files "
               "can be deleted for good by anyone with that one password.",
        "W17": f"`x_rfq_received_at` / `x_quote_sent_at` exist but appear in {views} Odoo screens; response time, win rate by speed "
               "and job profit were never reported until `BASELINE_METRICS.md`.",
        "W18": f"{fw} emails in purchasing@'s Sent forward a customer's whole drawing (title block “PROPERTY OF … - CONFIDENTIAL”); "
               f"{nda} customers are NDA-restricted in Odoo, a flag no screen shows.",
    }  # fmt: skip


def render(ev: dict[str, str]) -> str:
    rows = [f"| **{w}** | {TITLE[w]} | {ev[w]} | {SEE[w]} | {FIXED_IN[w]} |" for w in TITLE]
    return "\n".join([
        "# Pain points W1–W18, with evidence from the running system",
        "",
        f"_Generated {datetime.now():%Y-%m-%d %H:%M} by `scripts/pain_points.py`: every evidence cell is queried live "
        "from Odoo, the mailboxes (IMAP), the file share (SMB) or a container. Regenerate after `make demo-reset`._",
        "",
        "| ID | Weakness | Evidence (live) | See it yourself | Fixed in |",
        "|---|---|---|---|---|",
        *rows,
        "",
    ])  # fmt: skip


def main() -> None:
    o = Odoo("admin", env("ODOO_ADMIN_PASSWORD"))  # the audit needs screen definitions (ir.ui.view): IT's login
    ev = evidence(o, measure())
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(render(ev), encoding="utf-8", newline="\n")
    print(f"pain points: {len(ev)} weaknesses with live evidence -> {REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
