"""Run INSIDE the worker container by test_s8_actions.py (`python - <email_id> < s8_replay.py`): replay act's write
calls for one sent job with the person's REAL approval token, once unchanged (must change nothing: idempotent) and with
tampered content (must be refused by OPA inside the tool). Prints only the outcomes, never the token."""

import asyncio
import json
import sys
from typing import Any

from apex_agent.actions.approval import price_lines, reply_subject
from apex_agent.tools import call_tool
from apex_agent.worker import Worker

e = int(sys.argv[1])
w = Worker()
values = w.graph.graph.get_state(w.graph.config(e)).values
token, est = values["review"]["approval"], values["estimates"]
with w.conn.cursor() as cur:
    cur.execute("SELECT from_addr, subject FROM emails WHERE id = %s", (e,))
    mail = cur.fetchone()
    cur.execute("SELECT final_text FROM decisions WHERE email_id = %s", (e,))
    decided = cur.fetchone()
assert mail and decided, f"email {e} has no decision"
to, subject = mail
body = decided[0]
subj = reply_subject(values["draft"].get("subject"), subject)
lines = price_lines(est)


def call(server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    ok, res = asyncio.run(call_tool(server, tool, {"email_id": e, "approval": token, **args}, "act"))
    return {"ok": ok, "result": res if ok else str(res)[:300]}


quote = {"customer": {"email": to}, "validity_date": "2030-01-01", "note_html": ""}
cheaper = [{**ln, "breaks": [{**b, "unit_price": round(b["unit_price"] * 0.9, 2)} for b in ln["breaks"]]} for ln in lines]
print(json.dumps({
    "quotation_again": call("erp-mcp-write", "create_draft_quotation", {**quote, "lines": lines}),
    "quotation_cheaper": call("erp-mcp-write", "create_draft_quotation", {**quote, "lines": cheaper}),
    "reply_again": call("mail-mcp", "send_reply", {"to": to, "subject": subj, "body": body}),
    "reply_changed": call("mail-mcp", "send_reply", {"to": to, "subject": subj, "body": body + "\nPS: 10 % off today."}),
    "reply_elsewhere": call("mail-mcp", "send_reply", {"to": "someone@elsewhere.example", "subject": subj, "body": body}),
    "reply_for_another_job": asyncio.run(call_tool("mail-mcp", "send_reply", {"email_id": e + 1, "approval": token, "to": to,
                                                                            "subject": subj, "body": body}, "act"))[0],
}))  # fmt: skip
