"""The quote PDF (S8), rendered with WeasyPrint from the approved estimates only: no number is typed by a person or
written by the LLM. Price breaks, committed delivery date (and the rush option), validity, the material
price-protection clause, CO2 when the customer asked for it, and the assumptions the price rests on.
ponytail: the PDF is in English; the covering email is in the customer's language (a translated PDF is a later step).
"""

from datetime import date
from typing import Any

from jinja2 import Environment

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
@page { size: A4; margin: 18mm 16mm 20mm; @bottom-center { content: "Apex Precision Components · page " counter(page) " of " counter(pages); font-size: 8pt; color: #667; } }
body { font: 9.5pt/1.4 "DejaVu Sans", sans-serif; color: #1d2430; }
h1 { font-size: 16pt; margin: 0; } h2 { font-size: 11pt; margin: 16px 0 6px; border-bottom: 1px solid #ccd; padding-bottom: 2px; }
.head { display: flex; justify-content: space-between; } .muted { color: #5c6675; }
table { border-collapse: collapse; width: 100%; margin: 4px 0; } th, td { padding: 4px 6px; border-bottom: 1px solid #dde; text-align: left; vertical-align: top; }
th { background: #f1f3f6; font-size: 8.5pt; } td.n, th.n { text-align: right; }
.box { border: 1px solid #ccd; padding: 6px 8px; margin: 6px 0; background: #fafbfc; }
</style></head><body>
<div class="head"><div><h1>Apex Precision Components</h1><div class="muted">CNC machining · rfq@apex.local</div></div>
<div style="text-align:right"><strong>QUOTATION {{ q.number }}</strong><br>Date {{ q.date }}<br>Valid until <strong>{{ q.valid_until }}</strong></div></div>
<p><strong>To:</strong> {{ q.customer }}<br><span class="muted">Your request: “{{ q.subject }}” ({{ q.received }})</span></p>
{% for p in q.parts %}
<h2>{{ p.code }}{% if p.title %} · {{ p.title }}{% endif %}</h2>
<table><tr><th class="n">Quantity</th><th class="n">Unit price (USD)</th><th class="n">Total (USD)</th></tr>
{% for b in p.breaks %}<tr><td class="n">{{ b.qty }}</td><td class="n">{{ "{:,.2f}".format(b.unit_price) }}</td><td class="n">{{ "{:,.2f}".format(b.unit_price * b.qty) }}</td></tr>{% endfor %}
</table>
<p>Committed delivery: <strong>{{ p.committed }}</strong> (ex works) · Rush option: {{ p.rush }} at +{{ p.rush_premium_pct }} %.</p>
<p class="muted">Based on: {{ p.assumptions | join("; ") }}.</p>
{% if p.co2 %}<div class="box"><strong>Carbon footprint (estimate):</strong> {{ p.co2.per_part_kg }} kg CO₂e per part · {{ p.co2.per_lot_kg }} kg per lot.<br><span class="muted">{{ p.co2.methodology }}</span></div>{% endif %}
{% if p.clause %}<div class="box">{{ p.clause }}</div>{% endif %}
{% endfor %}
{% if q.not_quoted %}<h2>Not quoted</h2><p>{{ q.not_quoted | join("; ") }}</p>{% endif %}
<h2>Terms</h2>
<p>Prices in USD, ex works Apex, excluding taxes and freight. Validity as stated above; delivery dates are confirmed when the order is received.
Material price protection applies as stated per part.</p>
<p class="muted">Approved by {{ q.approved_by }} on {{ q.date }}. Reference {{ q.reference }}.</p>
</body></html>"""

_env = Environment(autoescape=True)


def quote_data(*, number: str, reference: str, customer: str, subject: str, received: str, approved_by: str,
               estimates: list[dict[str, Any]], extraction: dict[str, Any], not_quoted: list[str]) -> dict[str, Any]:  # fmt: skip
    """The PDF's content, taken field by field from the approved estimates and the extraction."""
    by_code = {p["part_code"]: {k: f.get("value") for k, f in p["fields"].items()} for p in extraction.get("parts", [])}
    parts = []
    for e in sorted(estimates, key=lambda e: e["code"]):
        f, part = by_code.get(e["code"], {}), e["part"]
        assumptions = [f"material {f.get('material_text') or part['material']}",
                       f"drawing {f.get('drawing_no') or e['code']} rev {f.get('rev') or '-'}",
                       f"tightest tolerance ±{part['tolerance_mm']} mm", f"surface Ra {part['ra_um']} µm"]  # fmt: skip
        if part.get("outside"):
            assumptions.append("outside processes: " + ", ".join(part["outside"]))
        parts.append({"code": e["code"], "title": f.get("title"), "breaks": e["breaks"], "committed": e["delivery"]["committed"],
                      "rush": e["delivery"]["rush"], "rush_premium_pct": e["delivery"]["rush_premium_pct"], "assumptions": assumptions,
                      "clause": e["terms"]["clause"], "co2": e["co2"] if e["co2"].get("include_in_quote") else None})  # fmt: skip
    valid = min((e["terms"]["validity_date"] for e in estimates), default="")
    return {"number": number, "reference": reference, "customer": customer, "subject": subject, "received": received,
            "approved_by": approved_by, "date": date.today().isoformat(), "valid_until": valid, "parts": parts,
            "not_quoted": not_quoted}  # fmt: skip


def render(q: dict[str, Any]) -> bytes:
    from weasyprint import HTML as WeasyHTML  # heavy import, only where a PDF is made

    return bytes(WeasyHTML(string=_env.from_string(HTML).render(q=q)).write_pdf())
