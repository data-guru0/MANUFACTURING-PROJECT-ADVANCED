"""The `act` node's side effects (S8), in order, each one idempotent and each one checked by OPA INSIDE the write tool
against the person's signed approval:

  1. quote: a DRAFT quotation in Odoo (erp-mcp-write, as erp_integration): lines, price breaks as options, validity,
     terms, the drawings attached; a new customer or part is created there too
  2. quote: the quote PDF (WeasyPrint, from the approved estimates)
  3. the reply from rfq@ (mail-mcp), threaded under the customer's RFQ, with the PDF; a copy in rfq@'s Sent folder
  4. quote: x_quote_sent_at and the PDF on the Odoo quotation

A decision without a signed approval (the operator's CLI) is recorded but never sends anything.
"""

import asyncio
import base64
import email
from datetime import UTC, datetime
from email import policy as email_policy
from typing import Any

from psycopg.types.json import Json

from apex_agent import audit
from apex_agent.actions import quote_pdf
from apex_agent.actions.approval import price_lines, reply_subject
from apex_agent.llm.client import NeedsHuman
from apex_agent.tools import call_tool

NODE = "act"


def tool(server: str, name: str, args: dict[str, Any]) -> Any:
    ok, res = asyncio.run(call_tool(server, name, args, NODE))
    if not ok:  # OPA refused, Odoo or the mail server down...: the job waits and is retried, a person sees why
        raise NeedsHuman(f"{server} {name}: {str(res)[:300]}")
    return res


def b64file(name: str, mimetype: str, data: bytes) -> dict[str, str]:
    return {"name": name, "mimetype": mimetype, "data_b64": base64.b64encode(data).decode()}


def run(w: Any, s: dict[str, Any], final_text: str) -> dict[str, Any]:
    """Returns the outcome (what was done) and the job's final status."""
    e, d, draft = s["email_id"], s["review"], s["draft"]
    token = d.get("approval")
    if d["action"] == "reject":
        return {"status": "done", "sent": False, "why": "rejected: nothing is sent"}
    if not token:
        return {"status": "done", "sent": False, "why": "decided outside the review screen (no signed approval): recorded only"}
    if not final_text.strip():
        return {"status": "done", "sent": False, "why": "no email text to send"}
    with w.conn.cursor() as cur:
        cur.execute("SELECT from_addr, from_name, subject, raw_sha256, sent_at, ingested_at FROM emails WHERE id = %s", (e,))
        to, from_name, subject, sha, sent_at, ingested_at = cur.fetchone()
        cur.execute(
            "SELECT filename, true_type, sha256 FROM attachments WHERE email_id = %s AND verdict = 'clean' ORDER BY id", (e,)
        )
        drawings = cur.fetchall()
    original = email.message_from_bytes(w.store.get(f"raw/{sha}.eml"), policy=email_policy.default)
    quote = d["action"] in ("approve", "edit") and draft.get("mode") == "quote" and bool(s.get("estimates"))
    done: dict[str, Any] = {"status": "sent", "sent": True}
    pdf = None
    if quote:
        lines = price_lines(s["estimates"])
        x = s["extraction"]
        titles = {p["part_code"]: (p["fields"].get("title") or {}).get("value") for p in x.get("parts", [])}
        est = s["estimates"]
        note = "<p>" + "</p><p>".join(
            [f"{q['code']}: committed delivery {q['delivery']['committed']} (rush {q['delivery']['rush']}, +{q['delivery']['rush_premium_pct']} %)" for q in est]
            + [q["terms"]["clause"] for q in est if q["terms"].get("clause")]  # no index for the material: no clause
            + [f"{q['code']}: {q['co2']['per_part_kg']} kg CO2e per part (estimate, not a certified footprint)" for q in est if q["co2"].get("include_in_quote")]
            + [f"Approved by {d.get('name') or d.get('by')} ({d['action']}) in the Apex review screen; RFQ email #{e}."]
        ) + "</p>"  # fmt: skip
        so = tool("erp-mcp-write", "create_draft_quotation", {
            "email_id": e, "approval": token, "customer": {"ref": s["gate"].get("customer"), "name": from_name, "email": to},
            "lines": lines, "validity_date": min(q["terms"]["validity_date"] for q in est), "note_html": note,
            "rfq_received_at": (sent_at or ingested_at).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "descriptions": {k: v for k, v in titles.items() if v},
            "attachments": [b64file(f, t or "application/octet-stream", w.store.get(f"files/{h}")) for f, t, h in drawings],
        })  # fmt: skip
        done["quotation"] = so["name"]
        pdf = quote_pdf.render(quote_pdf.quote_data(
            number=so["name"], reference=f"AGENT-RFQ-{e}", customer=f"{from_name or ''} <{to}>".strip(), subject=subject or "",
            received=str((sent_at or ingested_at).date()), approved_by=d.get("name") or d.get("by") or "", estimates=est,
            extraction=x, not_quoted=s.get("not_costed") or []))  # fmt: skip
        done["pdf_bytes"] = len(pdf)
    mail = tool("mail-mcp", "send_reply", {
        "email_id": e, "approval": token, "to": to, "subject": reply_subject(draft.get("subject"), subject), "body": final_text,
        "in_reply_to": str(original.get("Message-ID") or ""), "references": str(original.get("References") or ""),
        "attachments": [b64file(f"Apex quotation {done['quotation']}.pdf", "application/pdf", pdf)] if pdf else [],
    })  # fmt: skip
    done["message_id"] = mail["message_id"]
    if quote:
        marked = tool("erp-mcp-write", "mark_quote_sent", {
            "email_id": e, "approval": token, "sent_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "attachments": [b64file(f"Apex quotation {done['quotation']}.pdf", "application/pdf", pdf)] if pdf else [],
        })  # fmt: skip
        done["x_quote_sent_at"] = marked["x_quote_sent_at"]
    with w.conn.cursor() as cur:
        cur.execute("INSERT INTO actions (email_id, result) VALUES (%s, %s)", (e, Json(done)))
    audit.record("action", str(d.get("by")), {"approved_action": d["action"], **done}, e)  # S9: what went out, allowed by whom
    w.conn.commit()
    return done
