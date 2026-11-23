"""The as-is baseline, measured from the running legacy system -> docs/legacy/BASELINE_METRICS.md.

Sources, exactly as an FDE would get them at the client:
- **IMAP** (rfq@): when each RFQ arrived (Archive) and when the quote went out (Sent), matched by the
  customer's reference in the subject. Plus today's unanswered inbox.
- **Odoo** (JSON-RPC): outcome of each quote, lost reasons, promised vs actual delivery, and the
  actual job cost (purchase orders + work-order minutes x machine rate + 15 % overhead).

Metrics: median / p90 response time, win rate, win rate by response-time bucket, on-time delivery,
share of lost quotes with a known reason, loss-making jobs. Everything below the students' marker
in the existing report is kept (their hand-timed quotes).

ponytail: calendar days, not working days; overhead is the legacy model's flat 15 %.

Usage: uv run python -m scripts.baseline_metrics
"""

import imaplib
import re
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from legacy.mail.client import imap
from legacy.odoo.rpc import ROOT, Odoo, env

REPORT = ROOT / "docs" / "legacy" / "BASELINE_METRICS.md"
MARKER = "<!-- students-log: everything below this line is kept when the report is regenerated -->"
FMT = "%Y-%m-%d %H:%M:%S"
BUCKETS = [(0, 1, "< 1 day"), (1, 3, "1-3 days"), (3, 5, "3-5 days"), (5, 10, "5-10 days"), (10, 10_000, "> 10 days")]
OVERHEAD = 0.15
STUDENT_LOG = """
## Students' hand-quoting log (fill in during class)
Quote these three RFQs from `rfq@` **by hand**, following `SOP_MANUAL_QUOTING.md`, and write the minutes per step.

| Step (SOP) | RFQ D-10305-B (Rookfield, revision) | Anfrage D-20031-A (Krämer, German) | RFQ D-10227-A (Zephyr, repeat) |
|---|---|---|---|
| 1. Find and read the email | | | |
| 2. Read the drawing | | | |
| 3. Search Odoo for similar parts | | | |
| 4. Fill in the calculator | | | |
| 5. Ask Ramesh / apply his notes | | | |
| 6. Decide the delivery date | | | |
| 7. Enter the quote in Odoo | | | |
| 8. Email the quote | | | |
| **Total minutes** | | | |
"""


def odt(s: str) -> datetime:
    return datetime.strptime(s, FMT)


def internaldate(raw: bytes) -> datetime:
    t = imaplib.Internaldate2tuple(raw)
    assert t is not None
    return datetime.fromtimestamp(time.mktime(t), UTC).replace(tzinfo=None)


def mailbox_times(folder: str) -> dict[str, datetime]:
    """HIST ref -> INTERNALDATE (naive UTC) for every message in rfq@'s `folder`."""
    m = imap("rfq@apex.local")
    try:
        m.select(folder, readonly=True)
        _, data = m.search(None, "ALL")
        if not data[0]:
            return {}
        _, got = m.fetch(b",".join(data[0].split()).decode(), "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
    finally:
        m.logout()
    out = {}
    for part in got:
        if isinstance(part, tuple):
            ref = re.search(rb"HIST-\d{4}", part[1])
            if ref:
                out[ref.group().decode()] = internaldate(part[0])
    return out


def inbox_waiting() -> tuple[int, float]:
    """Unanswered emails in rfq@'s inbox and the age (days) of the oldest."""
    m = imap("rfq@apex.local")
    try:
        _, data = m.select("INBOX", readonly=True)
        n = int(data[0] or 0)
        if not n:
            return 0, 0.0
        _, got = m.fetch("1", "(INTERNALDATE)")
    finally:
        m.logout()
    head = got[0]
    first = head[0] if isinstance(head, tuple) else head
    assert isinstance(first, bytes)
    return n, (datetime.now(UTC).replace(tzinfo=None) - internaldate(first)).total_seconds() / 86400


@dataclass
class Baseline:
    response_days: dict[str, float]  # ref -> days from RFQ arrival to quote sent (from IMAP)
    imap_matches_odoo: float  # share of quotes whose IMAP times equal Odoo's fields (to the minute)
    sos: list[dict[str, Any]]
    won: int
    lost: int
    open: int
    by_bucket: list[tuple[str, int, int]]  # label, decided quotes, won
    delivered: int
    on_time: int
    lost_with_reason: int
    jobs: list[tuple[str, float, float]]  # SO name, revenue, actual cost (done jobs only)
    inbox: tuple[int, float]

    @property
    def win_rate(self) -> float:
        return self.won / (self.won + self.lost)

    @property
    def loss_jobs(self) -> list[tuple[str, float, float]]:
        return [j for j in self.jobs if j[2] > j[1]]


def job_costs(o: Odoo, won: list[dict[str, Any]]) -> list[tuple[str, float, float]]:
    names = [s["name"] for s in won]
    rate = {w["id"]: w["costs_hour"] for w in o.call("mrp.workcenter", "search_read", [], fields=["costs_hour"])}
    done = {m["origin"] for m in o.call("mrp.production", "search_read", [("origin", "in", names), ("state", "=", "done")],
                                        fields=["origin"])}  # fmt: skip
    buy: dict[str, float] = {}
    for p in o.call("purchase.order", "search_read", [("origin", "in", names), ("state", "in", ["purchase", "done"])],
                    fields=["origin", "amount_untaxed"]):  # fmt: skip
        buy[p["origin"]] = buy.get(p["origin"], 0.0) + p["amount_untaxed"]
    machine: dict[str, float] = {}
    for w in o.call("mrp.workorder", "search_read", [("production_id.origin", "in", list(done))],
                    fields=["production_id", "duration", "workcenter_id"]):  # fmt: skip
        origin = w["production_id"][1]
        machine[origin] = machine.get(origin, 0.0) + w["duration"] / 60 * rate[w["workcenter_id"][0]]
    mo_name = {m["name"]: m["origin"] for m in o.call("mrp.production", "search_read", [("origin", "in", list(done))],
                                                      fields=["name", "origin"])}  # fmt: skip
    by_so: dict[str, float] = {}
    for mo, cost in machine.items():
        by_so[mo_name.get(mo, mo)] = by_so.get(mo_name.get(mo, mo), 0.0) + cost
    out = []
    for s in won:
        if s["name"] in done:
            cost = (buy.get(s["name"], 0.0) + by_so.get(s["name"], 0.0)) * (1 + OVERHEAD)
            out.append((s["name"], float(s["amount_untaxed"]), round(cost, 2)))
    return out


def measure() -> Baseline:
    o = Odoo("office", env("ODOO_OFFICE_PASSWORD"))
    sos: list[dict[str, Any]] = o.call("sale.order", "search_read", [("client_order_ref", "=like", "HIST-%")], fields=[
        "name", "client_order_ref", "state", "amount_untaxed", "commitment_date", "effective_date",
        "x_rfq_received_at", "x_quote_sent_at", "x_lost_reason"], order="client_order_ref")  # fmt: skip
    got, sent = mailbox_times("Archive"), mailbox_times("Sent")
    resp = {r: (sent[r] - got[r]).total_seconds() / 86400 for r in got if r in sent}
    same = sum(1 for s in sos if s["client_order_ref"] in resp
               and abs((got[s["client_order_ref"]] - odt(s["x_rfq_received_at"])).total_seconds()) < 60
               and abs((sent[s["client_order_ref"]] - odt(s["x_quote_sent_at"])).total_seconds()) < 60)  # fmt: skip
    won = [s for s in sos if s["state"] == "sale"]
    lost = [s for s in sos if s["state"] == "cancel"]
    buckets = []
    for lo, hi, label in BUCKETS:
        decided = [s for s in won + lost if lo <= resp.get(s["client_order_ref"], -1) < hi]
        buckets.append((label, len(decided), sum(1 for s in decided if s["state"] == "sale")))
    delivered = [s for s in won if s["effective_date"] and s["commitment_date"]]
    return Baseline(
        response_days=resp, imap_matches_odoo=same / len(sos), sos=sos, won=len(won), lost=len(lost),
        open=len(sos) - len(won) - len(lost), by_bucket=buckets, delivered=len(delivered),
        on_time=sum(1 for s in delivered if odt(s["effective_date"]) <= odt(s["commitment_date"])),
        lost_with_reason=sum(1 for s in lost if s["x_lost_reason"]), jobs=job_costs(o, won), inbox=inbox_waiting(),
    )  # fmt: skip


def pct(a: float, b: float) -> str:
    return f"{100 * a / b:.1f} %" if b else "n/a"


def render(b: Baseline) -> str:
    days = sorted(b.response_days.values())
    p90 = statistics.quantiles(days, n=10)[-1]
    loss = b.loss_jobs
    lines = [
        "# Baseline metrics: Apex quoting today (as-is)",
        "",
        f"_Generated {datetime.now():%Y-%m-%d %H:%M} by `scripts/baseline_metrics.py` from the running legacy system: "
        "IMAP (rfq@ Archive and Sent) and Odoo. Regenerate any time; do not edit the numbers by hand._",
        "",
        "## Headline numbers",
        "| Metric | Value | Source | Weakness |",
        "|---|---|---|---|",
        f"| Quotes measured | {len(days)} of {len(b.sos)} | IMAP Archive ↔ Sent, matched by customer ref | |",
        f"| **Median response time** (RFQ in → quote out) | **{statistics.median(days):.1f} days** | IMAP INTERNALDATE | W1 |",
        f"| **p90 response time** | **{p90:.1f} days** | IMAP | W1 |",
        f"| **Win rate** (won / decided) | **{pct(b.won, b.won + b.lost)}** ({b.won} won, {b.lost} lost, {b.open} open) | Odoo | |",
        f"| **On-time delivery** | **{pct(b.on_time, b.delivered)}** ({b.on_time} of {b.delivered} delivered orders) | Odoo delivered vs promised date | W7 |",
        f"| **Lost quotes with a known reason** | **{pct(b.lost_with_reason, b.lost)}** ({b.lost_with_reason} of {b.lost}) | Odoo `x_lost_reason` | W9 |",
        f"| **Loss-making jobs** | **{len(loss)} of {len(b.jobs)} finished jobs** (total loss ${sum(c - r for _, r, c in loss):,.0f}) | Odoo POs + work-order time x rate + 15 % | W8, W11, W14 |",
        f"| RFQs waiting in the shared inbox now | {b.inbox[0]} (oldest {b.inbox[1]:.1f} days) | IMAP INBOX | W1 |",
        f"| Mail and ERP agree on the dates | {pct(b.imap_matches_odoo, 1)} of quotes | IMAP vs Odoo `x_rfq_received_at` / `x_quote_sent_at` (a sanity check) | |",
        "",
        "## Win rate by response time",
        "Faster answers win more often. This is the business case for W1.",
        "",
        "| Response time | Decided quotes | Won | Win rate |",
        "|---|---|---|---|",
        *[f"| {label} | {n} | {w} | {pct(w, n)} |" for label, n, w in b.by_bucket],
        "",
        "## The 10 worst jobs (actual cost vs price)",
        "| Sales order | Price | Actual cost | Result |",
        "|---|---|---|---|",
        *[f"| {n} | ${r:,.2f} | ${c:,.2f} | {'−' if c > r else '+'}${abs(r - c):,.2f} |" for n, r, c in sorted(b.jobs, key=lambda j: j[1] - j[2])[:10]],
        "",
        "## How each number is computed",
        "- **Response time:** for each historical quote, the INTERNALDATE of the customer's RFQ in `rfq@/Archive` and of "
        "Apex's quotation in `rfq@/Sent`, matched by the customer reference (`HIST-xxxx`) in the subject. Calendar days.",
        "- **Win rate:** confirmed sales orders / (confirmed + cancelled) among the historical quotes.",
        "- **On-time:** a won order is on time if its delivery was validated on or before the promised `commitment_date`.",
        "- **Actual job cost:** all confirmed purchase orders for the job (material, subcontracting) + the recorded work-order "
        "minutes x the work center's hourly rate, + 15 % overhead. Only finished jobs (manufacturing order done).",
        "",
        MARKER,
    ]  # fmt: skip
    return "\n".join(lines) + "\n"


def main() -> None:
    b = measure()
    text = render(b)
    kept = REPORT.read_text(encoding="utf-8").split(MARKER, 1)[1] if REPORT.exists() and MARKER in REPORT.read_text(
        encoding="utf-8") else STUDENT_LOG  # fmt: skip
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text + kept, encoding="utf-8", newline="\n")
    days = sorted(b.response_days.values())
    print(f"baseline: median response {statistics.median(days):.1f} d, win rate {pct(b.won, b.won + b.lost)}, "
          f"on-time {pct(b.on_time, b.delivered)}, lost with reason {pct(b.lost_with_reason, b.lost)}, "
          f"loss-making jobs {len(b.loss_jobs)}/{len(b.jobs)} -> {REPORT.relative_to(ROOT)}")  # fmt: skip


if __name__ == "__main__":
    main()
