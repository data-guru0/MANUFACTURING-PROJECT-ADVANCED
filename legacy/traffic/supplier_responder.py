"""The suppliers, simulated over real email: they answer Apex's price requests.

Every suppliers.local mailbox is polled. An unread email from apex.local is read for what is asked:
- material suppliers: any name of a material they stock ("6061-T6", "SS304", "Delrin", ...);
- finishers: the process ("anodiz", "powder", "plating", "passivat", "heat treat");
- the subcontractor (Precision Partners): drawing numbers like D-10023-A.
The price is the supplier's Odoo price-list entry (product.supplierinfo) moved by market noise
(-3 % .. +12 %), the lead time that entry's delay +-; the subcontractor bids 65-85 % of Apex's last
quoted price for that drawing. Half the suppliers answer in the email text, half with a PDF quote.
Unknown requests get a polite "we don't supply that".

Runs as the `suppliers` container; `--once` polls one round. Reads Odoo with the shared `office`
login (W3), the simulation's source of truth for "the supplier's own price list".
"""

import argparse
import hashlib
import os
import random
import re
from email.message import Message
from typing import Any

from legacy.mail.client import handle_unseen_from_apex, poll_forever, reply, text_of
from legacy.odoo.rpc import Odoo, env
from legacy.odoo.setup_odoo import MATERIALS, OUTSIDE_PROCESSES
from legacy.seed.history import GRADE_VARIANTS, Supplier, generate

PROCESS_WORDS = {"OSP-ANODIZE": ["anodiz", "eloxi"], "OSP-PLATING": ["plating", "zinc plat"], "OSP-POWDER": ["powder"],
                 "OSP-PASSIVATE": ["passivat"], "OSP-HEATTREAT": ["heat treat", "harden"]}  # fmt: skip
DRAWING = re.compile(r"\bD-\d{5}-[A-Z]\b")
QTY = re.compile(r"(\d[\d,]*)\s*(?:pcs|pieces|kg|Stk|piezas)", re.I)
NAMES = {c: n for c, n, *_ in MATERIALS} | {c: n for c, n, *_ in OUTSIDE_PROCESSES}


def wanted_codes(text: str, stocked: list[str]) -> list[str]:
    low = text.lower()
    out = []
    for code in stocked:
        words = PROCESS_WORDS.get(code) or [v.lower() for v in GRADE_VARIANTS.get(code, [])] + [code.lower()]
        if any(w in low for w in words):
            out.append(code)
    return out


class Pricing:
    def __init__(self) -> None:
        self.odoo = Odoo("office", env("ODOO_OFFICE_PASSWORD"))

    def pricelist(self, supplier_key: str) -> dict[str, dict[str, Any]]:
        rows = self.odoo.call("product.supplierinfo", "search_read", [("partner_id.ref", "=", supplier_key)],
                              fields=["price", "delay", "product_tmpl_id", "date_start"], order="id")  # fmt: skip
        tmpl = {r["product_tmpl_id"][0] for r in rows}
        codes = {p["id"]: p["default_code"] for p in self.odoo.call("product.template", "read", list(tmpl),
                                                                    fields=["default_code"])}  # fmt: skip
        return {codes[r["product_tmpl_id"][0]]: r for r in rows}  # the newest entry per product wins

    def last_quoted(self, code: str) -> float | None:
        rows = self.odoo.call("sale.order.line", "search_read", [("product_id.default_code", "=", code)],
                              fields=["price_unit"], order="id desc", limit=1)  # fmt: skip
        return float(rows[0]["price_unit"]) if rows else None


def offer(s: Supplier, msg: Message, pricing: Pricing) -> tuple[list[str], str]:
    """Quote lines, and a one-line summary for the log. All arithmetic here; nothing guessed by an LLM."""
    text = f"{msg.get('Subject', '')}\n{text_of(msg)}"
    rng = random.Random(str(msg.get("Message-ID", "")))
    qty = QTY.search(text)
    qty_note = f" for {qty.group(0)}" if qty else ""
    lines = []
    if s.kind == "subcon":
        names = [str(p.get_filename() or "") for p in msg.walk()]
        for code in sorted(set(DRAWING.findall(text + " " + " ".join(names)))):
            last = pricing.last_quoted(code)
            if last:
                lines.append(f"{code}: {last * rng.uniform(0.65, 0.85):.2f} USD per piece{qty_note}, "
                             f"lead time {rng.randint(10, 25)} working days")  # fmt: skip
    else:
        pl = pricing.pricelist(s.key)
        for code in wanted_codes(text, list(pl)):
            row = pl[code]
            unit = "kg" if code.startswith("MAT-") else "piece"
            lines.append(f"{NAMES.get(code, code)} ({code}): {row['price'] * rng.uniform(0.97, 1.12):.2f} USD per {unit}"
                         f"{qty_note}, lead time {max(3, int(row['delay']) + rng.randint(-2, 5))} days")  # fmt: skip
    return lines, "; ".join(lines) or "nothing we supply"


def text_pdf(rows: list[str]) -> bytes:
    """A one-page PDF of plain text lines (Helvetica), stdlib only.
    ponytail: no PDF library in the responder image (69 MB wheel); enough for a supplier's quotation."""

    def esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    ops = "".join(f"BT /F1 {14 if i == 0 else 11} Tf 50 {760 - 18 * i} Td ({esc(r)}) Tj ET\n" for i, r in enumerate(rows))
    stream = ops.encode("latin-1", "replace")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream"]  # fmt: skip
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def quote_pdf(s: Supplier, number: str, lines: list[str]) -> bytes:
    return text_pdf([f"{s.name} - QUOTATION {number}", "To: Apex Precision Components, Purchasing", "", *lines, "",
                     "Prices valid 14 days. Material surcharges may apply."])  # fmt: skip


def respond_once(suppliers: list[Supplier] | None = None) -> int:
    suppliers = suppliers or generate().suppliers
    pricing = Pricing()
    done = 0
    for s in suppliers:

        def handle(msg: Message, s: Supplier = s) -> None:
            lines, summary = offer(s, msg, pricing)
            mid = str(msg.get("Message-ID", ""))
            number = f"Q-{int(hashlib.sha256(mid.encode()).hexdigest()[:6], 16) % 90000 + 10000}"
            if not lines:
                body = "Thank you for your enquiry. Unfortunately we do not supply what you asked for."
                reply(msg, s.email, s.name, f"{body}\n\nBest regards,\n{s.name}")
            elif int(hashlib.sha256(s.key.encode()).hexdigest(), 16) % 2:  # this supplier sends PDFs
                reply(msg, s.email, s.name, f"Please find our quotation {number} attached.\n\nBest regards,\n{s.name}",
                      [(f"Quotation_{number}.pdf", quote_pdf(s, number, lines), "application/pdf")])  # fmt: skip
            else:
                offer_text = "\n".join(f"- {ln}" for ln in lines)
                reply(msg, s.email, s.name, f"Thank you for your enquiry. Our offer {number}:\n{offer_text}\n"
                      f"Prices valid 14 days.\n\nBest regards,\n{s.name}")  # fmt: skip
            print(f"suppliers: {s.name} <- {msg.get('Subject')!s:.50}  => {summary}")

        done += handle_unseen_from_apex(s.email, handle)
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    suppliers = generate().suppliers
    if a.once:
        print(f"suppliers: answered {respond_once(suppliers)} emails")
    else:
        poll_forever("suppliers", lambda: respond_once(suppliers), float(os.environ.get("RESPONDER_INTERVAL", "10")))


if __name__ == "__main__":
    main()
