"""S17 evaluation (no LLM): supplier replies extracted right; nothing confidential left Apex; NDA / export blocked.

1. Extraction vs truth: the simulated suppliers write each offer line as "<name> (<CODE>): <price> USD per <unit> ...,
   lead time <n> days" (legacy/traffic/supplier_responder.py). That line, parsed from the stored reply (text + PDF), is
   the truth for what the LLM extracted.
2. Confidentiality: log into every supplier MAILBOX and read what Apex actually sent them (body + every PDF's text):
   the customer's name, email and domain must not appear.
3. Blocks: every RFQ of an NDA-restricted customer or an export-controlled job is `blocked`, none was ever sent.
Writes docs/fde/SUPPLIERS_EVAL.md.

Usage: uv run python -m scripts.suppliers_eval
"""

import email
import json
import re
import subprocess
import unicodedata
from email import policy
from pathlib import Path
from typing import Any

import pymupdf

from legacy.mail.client import fetch, imap

REPO = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "apex",
    "-f",
    str(REPO / "docker-compose.legacy.yml"),
    "-f",
    str(REPO / "docker-compose.agent.yml"),
]
LINE = re.compile(
    r"\((?P<code>(?:MAT|OSP)-[A-Z0-9]+)\):\s*(?P<price>\d+\.\d+)\s*USD per (?P<unit>kg|piece).*?lead time (?P<lead>\d+) (?:working )?days"
)
TRUTH_CODE = r"""
import json, pymupdf, email
from email import policy
from apex_agent.knowledge import db
from apex_agent.ingest.store import Store
c, st = db.connect(), Store()
rows = c.execute('''SELECT r.id, r.email_id, r.code, r.vendor_name, r.status, r.quote, r.need, r.reply_email, m.raw_sha256,
                           g.sender->>'known_customer', e.from_addr, e.from_name
                    FROM supplier_rfqs r LEFT JOIN emails m ON m.id = r.reply_email JOIN emails e ON e.id = r.email_id
                    LEFT JOIN gate_results g ON g.email_id = r.email_id ORDER BY r.id''').fetchall()
out = []
for rid, job, code, vendor, status, quote, need, reply, raw, cust, addr, name in rows:
    text = ''
    if raw:
        msg = email.message_from_bytes(st.get(f'raw/{raw}.eml'), policy=policy.default)
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                text += part.get_content()
            elif part.get_content_type() == 'application/pdf':
                with pymupdf.open(stream=part.get_payload(decode=True), filetype='pdf') as d:
                    text += '\n'.join(p.get_text() for p in d)
    out.append({'id': rid, 'job': job, 'code': code, 'vendor': vendor, 'status': status, 'quote': quote, 'need': need,
                'reply_text': text, 'customer_ref': cust, 'customer_addr': addr, 'customer_name': name})
print(json.dumps(out, default=str))
"""


def fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()


def sent_to_suppliers(addresses: set[str]) -> list[dict[str, Any]]:
    """What Apex really sent: read each supplier's mailbox (as that supplier) for our RFQ Message-IDs."""
    out = []
    for addr in sorted(addresses):
        m = imap(addr)
        try:
            m.select("INBOX")
            typ, found = m.uid("SEARCH", "HEADER", "Message-ID", "apex-srfq-")  # UIDs, as fetch() uses
            for uid in found[0].split() if typ == "OK" and found and found[0] else []:
                msg = email.message_from_bytes(fetch(m, uid).as_bytes(), policy=policy.default)
                text = ""
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        text += part.get_content()
                    elif part.get_content_type() == "application/pdf":
                        with pymupdf.open(stream=part.get_payload(decode=True), filetype="pdf") as d:
                            text += "\n".join(p.get_text() for p in d) + json.dumps(d.metadata or {})
                out.append({"to": addr, "message_id": str(msg["Message-ID"]), "subject": str(msg["Subject"]), "text": text,
                            "attachments": [str(p.get_filename()) for p in msg.iter_attachments()]})  # fmt: skip
        finally:
            m.logout()
    return out


def evaluate() -> dict[str, Any]:
    r = subprocess.run(
        [*COMPOSE, "exec", "-T", "worker", "python", "-c", TRUTH_CODE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    if r.returncode:
        raise SystemExit(r.stderr[-1500:])
    rows = json.loads(r.stdout.strip().splitlines()[-1])
    res: dict[str, Any] = {"replies": 0, "lines_true": 0, "lines_extracted_right": 0, "wrong": [], "declines_right": 0,
                           "sent": 0, "leaks": [], "blocked": 0, "blocked_but_sent": [], "settled": []}  # fmt: skip
    for x in rows:
        if x["status"] == "blocked":
            res["blocked"] += 1
            continue
        if x["status"] != "replied":
            continue
        res["replies"] += 1
        truth = [
            {"code": m["code"], "unit_price_usd": float(m["price"]), "lead_days": int(m["lead"])}
            for m in LINE.finditer(x["reply_text"])
        ]
        got = [
            {"code": ln["code"], "unit_price_usd": ln["unit_price_usd"], "lead_days": ln["lead_days"]}
            for ln in (x["quote"] or {}).get("lines", [])
        ]
        if not truth:
            res["declines_right"] += bool((x["quote"] or {}).get("declined"))
        res["lines_true"] += len(truth)
        for t in truth:
            if t in got:
                res["lines_extracted_right"] += 1
            else:
                res["wrong"].append({"rfq": x["id"], "truth": t, "got": got})
        if (x["quote"] or {}).get("settled") and x["quote"].get("chosen") and x["quote"]["chosen"]["rfq_id"] == x["id"]:
            res["settled"].append({"job": x["job"], "code": x["code"], "vendor": x["vendor"], "price": x["quote"]["chosen"]["unit_price_usd"],
                                   "po": x["quote"]["odoo_po"]["name"]})  # fmt: skip
    vendors = {x["vendor"] for x in rows if x["status"] not in ("blocked", "draft")}
    addr_of: dict[str, str | None] = {x["vendor"]: None for x in rows}
    mail = subprocess.run([*COMPOSE, "exec", "-T", "app-postgres", "psql", "-U", "apex", "-d", "apex", "-Atc",
                           "SELECT DISTINCT vendor_name, vendor_email FROM supplier_rfqs"], capture_output=True, text=True, encoding="utf-8")  # fmt: skip
    for line in mail.stdout.splitlines():
        v, a = line.split("|")
        addr_of[v] = a
    sent = sent_to_suppliers({addr for v in vendors if (addr := addr_of.get(v))})
    res["sent"] = len(sent)
    secrets: dict[int, list[str]] = {}
    for x in rows:
        s = [x["customer_name"], x["customer_addr"], (x["customer_addr"] or "").split("@")[-1]]
        secrets.setdefault(x["job"], [])
        secrets[x["job"]] += [v for v in s if v] + [w for w in (x["customer_name"] or "").split() if len(w) >= 5]
    for msg in sent:
        job = int(re.search(r"apex-srfq-(\d+)-", msg["message_id"]).group(1))  # type: ignore[union-attr]
        rfq_job = next((x["job"] for x in rows if x["id"] == job), None)
        for sec in secrets.get(rfq_job or -1, []):
            if fold(sec) in fold(msg["text"] + msg["subject"]):
                res["leaks"].append({"to": msg["to"], "secret": sec})
        if any(x["id"] == job and x["status"] == "blocked" for x in rows):
            res["blocked_but_sent"].append(msg["message_id"])
    res["sent_with_drawing"] = sum(1 for m in sent if m["attachments"])
    return res


def main() -> None:
    r = evaluate()
    (REPO / "docs" / "fde" / "SUPPLIERS_EVAL.md").write_text("\n".join([
        "# Supplier RFQs (S17)", "",
        "_Generated by `uv run python -m scripts.suppliers_eval`. Truth: the simulated suppliers' own offer lines "
        "(legacy/traffic/supplier_responder.py), parsed from the stored replies; confidentiality: read from the supplier "
        "mailboxes. No LLM._", "",
        f"- Supplier replies: **{r['replies']}**; offer lines extracted right: **{r['lines_extracted_right']} of {r['lines_true']}**; "
        f"declines recognised: {r['declines_right']}",
        f"- RFQs sent: **{r['sent']}** ({r['sent_with_drawing']} with the redacted drawing); customer name / email / domain found "
        f"in what suppliers received: **{len(r['leaks'])}** (must be 0)",
        f"- RFQs blocked (NDA-restricted customer or export-controlled job): **{r['blocked']}**; blocked but sent: "
        f"**{len(r['blocked_but_sent'])}** (must be 0)", "",
        "| Job | Product | Chosen supplier | Price | Odoo draft PO |", "|---|---|---|---|---|",
        *[f"| {s['job']} | {s['code']} | {s['vendor']} | ${s['price']:.2f} | {s['po']} |" for s in r["settled"]], ""]), encoding="utf-8")  # fmt: skip
    print(json.dumps(r, default=str))


if __name__ == "__main__":
    main()
