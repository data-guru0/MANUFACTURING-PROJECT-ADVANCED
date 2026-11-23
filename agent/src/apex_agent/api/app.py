"""The review screen (S7): FastAPI + HTMX behind Envoy at https://localhost:8448.

Who gets in is decided BEFORE a request reaches this app: Envoy logs the user in with Keycloak (oauth2 filter), verifies
the token (jwt_authn) and asks OPA per route (ext_authz, apex/ingress). This app checks again, because it never trusts
the network: it verifies the token itself (Keycloak's published keys) and asks OPA (apex/review) before every decision.

Least privilege: the screen never runs the graph. A decision is written to `jobs.decision` (status 'decided') and the
worker resumes the job with it, where QuoteGraph.resume asks OPA a third time. So this process holds no LLM key, no
mail login and no write tool: its Postgres role may read the review tables and update only jobs(status, decision),
and its MinIO user may only read raw/ and files/.

Run: uvicorn apex_agent.api.app:app (the `review-ui` service).
"""

import email
import io
import json
import re
import uuid
from datetime import UTC, datetime
from email import policy as email_policy
from pathlib import Path
from typing import Annotated, Any

import httpx
import jwt
import psycopg
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from psycopg.types.json import Json

from apex_agent import audit as audit_log
from apex_agent import opa
from apex_agent.actions import approval
from apex_agent.ingest.store import Store
from apex_agent.security import content
from apex_agent.settings import ReviewSettings, load_as

HERE = Path(__file__).parent
S = load_as(ReviewSettings)
JWKS = jwt.PyJWKClient(S.jwks_url, cache_keys=True)
STAFF = {"estimator", "senior_estimator", "planner", "auditor", "admin"}
DECIDERS = {"estimator", "senior_estimator"}
ACTIONS = {"approve", "edit", "recost", "clarify", "reject"}

app = FastAPI(title="Apex review", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")  # autoescape on: customer text is untrusted

# Provenance -> the system it came from (the reviewer clicks through to the source of a number).
PROVENANCE_LINKS = [
    (re.compile(r"cycle-time model (\d{8}-\d{6})"), r"https://localhost:8447/browser/apex/models%2Fcycle_time%2F\1%2F"),
    (re.compile(r"Odoo price list"), "http://127.0.0.1:8069/web#model=product.supplierinfo&view_type=list"),
    (re.compile(r"\(Odoo[ )]"), "http://127.0.0.1:8069/web#model=mrp.workcenter&view_type=list"),
    (re.compile(r"Pink Sheet"), "https://www.worldbank.org/en/research/commodity-markets"),
]


def source_link(text: str) -> str | None:
    """The first matching source wins (a material line cites the price list before the density)."""
    return next((m.expand(url) for rx, url in PROVENANCE_LINKS if (m := rx.search(text))), None)


templates.env.globals["source_link"] = source_link
templates.env.filters["money"] = lambda v: f"${v:,.2f}"
templates.env.filters["js"] = lambda v: json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Any:
    r = await call_next(request)
    r.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; object-src 'self'; frame-src 'self'; "
                                            "style-src 'self' 'unsafe-inline'; frame-ancestors 'self'")  # fmt: skip
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "same-origin"
    return r


# ---- who is asking
class User:
    def __init__(self, claims: dict[str, Any]) -> None:
        self.username: str = claims["preferred_username"]
        self.name: str = claims.get("name") or self.username
        self.roles: list[str] = sorted(set(claims.get("realm_access", {}).get("roles", [])) & STAFF)
        self.decides = bool(set(self.roles) & DECIDERS) and "admin" not in self.roles


def user(request: Request) -> User:
    """Verify the Keycloak access token Envoy forwards (signature, expiry, issuer, the client it was issued to)."""
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    try:
        key = JWKS.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=S.issuer, options={"verify_aud": False})
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"invalid token: {e}") from None
    if claims.get("azp") != S.client_id or not claims.get("preferred_username"):
        raise HTTPException(401, "token not issued to the review screen")
    u = User(claims)
    if not u.roles:
        raise HTTPException(403, "no Apex role")
    return u


Who = Annotated[User, Depends(user)]


def db() -> psycopg.Connection[dict[str, Any]]:
    # ponytail: one connection per request (local, a handful of reviewers); a psycopg_pool when there are more
    return psycopg.connect(S.db_url.get_secret_value(), autocommit=True, row_factory=dict_row)


def audit(event: str, **fields: Any) -> None:
    """One JSON line per decision on stdout (Docker Desktop -> Logs): who, what, which job. No customer text."""
    print(json.dumps({"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **fields}), flush=True)
    audit_log.record(
        "human_decision", str(fields.get("by") or "unknown"), {"event": event, **fields}, fields.get("email_id")
    )  # S9


# ---- screens
@app.get("/", response_class=HTMLResponse)
def queue(request: Request, me: Who) -> Any:
    with db() as conn:
        waiting = conn.execute("""
            SELECT j.email_id, j.status, e.from_name, e.from_addr, e.subject, e.ingested_at, r.route, r.value_usd, r.needs_senior,
                   (r.payload->'risk'->'priority'->>'score')::float AS score, r.payload->'risk'->'priority'->>'explain' AS why
            FROM jobs j JOIN emails e ON e.id = j.email_id JOIN review_items r USING (email_id)
            WHERE j.status IN ('awaiting_review', 'decided') OR (j.status = 'running' AND j.decision IS NOT NULL)
            ORDER BY score DESC NULLS LAST, e.ingested_at""").fetchall()  # S11: by priority (factors shown), not oldest first
        decided = conn.execute("""
            SELECT d.email_id, d.action, d.decided_by, d.reason, d.decided_at, e.subject, r.value_usd
            FROM decisions d JOIN emails e ON e.id = d.email_id LEFT JOIN review_items r USING (email_id)
            ORDER BY d.decided_at DESC LIMIT 25""").fetchall()
        manual = conn.execute("""
            SELECT j.email_id, e.from_name, e.from_addr, e.subject, j.finished_at FROM jobs j JOIN emails e ON e.id = j.email_id
            WHERE j.status = 'manual' ORDER BY j.finished_at DESC LIMIT 25""").fetchall()
        # S19: manual mode. The kill switch is off, or jobs are waiting for a dependency (AI, Odoo) that is down.
        outage = conn.execute("""SELECT count(*) AS waiting, min(started_at) AS since, max(error) AS why FROM jobs
                                 WHERE status = 'queued' AND error ~* '(AiUnavailable|AiDisabled|unreachable|refused|Quota|503|502)'""").fetchone()
    ai_on = opa.allowed(S.opa_url, "apex/system/ai_enabled", {"agent": "review-ui", "alias": "-"})
    return templates.TemplateResponse(request, "queue.html", {"me": me, "waiting": waiting, "decided": decided, "manual": manual,
                                                              "ai_on": ai_on, "outage": outage})  # fmt: skip


@app.get("/capacity", response_class=HTMLResponse)
def capacity_page(request: Request, me: Who) -> Any:
    """S14 (W7): the latest expected-load heatmap (written by the graph's retrieve step or `python -m apex_agent.costing.capacity`)."""
    with db() as conn:
        row = conn.execute("SELECT at, data FROM capacity_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return templates.TemplateResponse(request, "capacity.html", {"me": me, "snap": row["data"] if row else None,
                                                                 "at": row["at"] if row else None})  # fmt: skip


@app.post("/jobs/{email_id}/whatif", response_class=HTMLResponse)
def whatif_ask(request: Request, me: Who, email_id: int, question: Annotated[str, Form()] = "",
               session: Annotated[str, Form()] = "") -> Any:  # fmt: skip
    """S16: hand the question to the assistant's service with the person's own token (it verifies it again)."""
    if not question.strip() or not session:
        return result(request, 400, ["type a question"])
    try:
        r = httpx.post(f"{S.whatif_url}/ask", json={"email_id": email_id, "session": session, "question": question},
                       headers={"authorization": request.headers.get("authorization", "")}, timeout=180)  # fmt: skip
    except httpx.HTTPError as e:
        return result(request, 503, [f"the what-if assistant is not reachable ({type(e).__name__})"])
    if r.status_code != 200:
        return result(request, r.status_code, [f"the what-if assistant said: {r.text[:300]}"])
    return templates.TemplateResponse(request, "_whatif.html", {"q": question, "a": r.json(), "email_id": email_id})


@app.post("/jobs/{email_id}/whatif/apply", response_class=HTMLResponse)
def whatif_apply(request: Request, me: Who, email_id: int, log_id: Annotated[int, Form()], call: Annotated[int, Form()]) -> Any:
    """S16: a PERSON applies a recomputed price as an option. The numbers are read from the stored tool result (never
    from the form), the option is recorded and the click goes into the audit log."""
    with db() as conn:
        row = conn.execute("SELECT email_id, tools FROM whatif_log WHERE id = %s", (log_id,)).fetchone()
        if not row or row["email_id"] != email_id or not 0 <= call < len(row["tools"]):
            return result(request, 404, ["no such answer for this job"])
        c = row["tools"][call]
        if c["tool"] != "recompute_cost" or "new_unit_price" not in c["result"]:
            return result(request, 400, ["only a recomputed price can be applied as an option"])
        res = c["result"]
        conn.execute("""INSERT INTO whatif_options (email_id, log_id, part_code, changes, qty, unit_price, by_user)
                        VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                     (email_id, log_id, res["part"], json.dumps(res["changes"]), res["quantity"], res["new_unit_price"], me.username))  # fmt: skip
    audit_log.record("human_decision", me.username, {"event": "what-if option applied", "log_id": log_id, "part": res["part"],
                                                     "changes": res["changes"], "qty": res["quantity"], "unit_price": res["new_unit_price"]}, email_id)  # fmt: skip
    return result(request, 200, [f"option recorded: {res['part']} x {res['quantity']} at ${res['new_unit_price']:.2f} ({res['changes']}), by {me.username}"],
                  ok=True)  # fmt: skip


@app.post("/jobs/{email_id}/suppliers/{rfq_id}/send", response_class=HTMLResponse)
def supplier_send(request: Request, me: Who, email_id: int, rfq_id: int) -> Any:
    """S17: a person approves ONE supplier RFQ exactly as shown. OPA (apex/suppliers) is asked again here; only then
    is the text signed (action supplier_rfq, supplier_ok) for mail-mcp, which checks it once more. The worker sends."""
    with db() as conn:
        r = conn.execute("SELECT vendor_email, subject, body, status, need FROM supplier_rfqs WHERE id = %s AND email_id = %s",
                         (rfq_id, email_id)).fetchone()  # fmt: skip
        if not r:
            return result(request, 404, ["no such supplier RFQ for this job"])
        if r["status"] != "draft":
            return result(request, 409, [f"this RFQ is {r['status']}"])
        d = opa.decide(S.opa_url, "apex/suppliers/decision", {"nda_restricted": r["need"].get("nda_restricted"),
                                                              "export_control": r["need"].get("export_control")})  # fmt: skip
        if not isinstance(d, dict) or d.get("allow") is not True:
            return result(request, 403, (d or {}).get("deny") or ["policy service (OPA) unavailable"])
        token = approval.sign(S.approval_key.get_secret_value(), email_id=email_id, action="supplier_rfq", by=me.username,
                              roles=me.roles, needs_senior=False, senior=[], prices_sha="", supplier_ok=True, rfq_id=rfq_id,
                              text_sha=approval.text_sha256(r["vendor_email"], r["subject"], r["body"]))  # fmt: skip
        conn.execute("UPDATE supplier_rfqs SET status = 'approved', approval = %s, approved_by = %s WHERE id = %s AND status = 'draft'",
                     (token, me.username, rfq_id))  # fmt: skip
    audit_log.record(
        "human_decision", me.username, {"event": "supplier RFQ approved", "rfq_id": rfq_id, "to": r["vendor_email"]}, email_id
    )
    return result(request, 200, [f"approved: the worker sends it to {r['vendor_email']} within seconds"], ok=True)


@app.get("/jobs/{email_id}/suppliers/{rfq_id}/drawing")
def supplier_drawing(me: Who, email_id: int, rfq_id: int) -> Response:
    """S17: the redacted drawing exactly as the supplier would get it."""
    with db() as conn:
        r = conn.execute("SELECT attachment FROM supplier_rfqs WHERE id = %s AND email_id = %s", (rfq_id, email_id)).fetchone()
    if not r or not r["attachment"]:
        raise HTTPException(404, "no drawing for this RFQ")
    return Response(Store().get(r["attachment"]), media_type="application/pdf")


@app.get("/digest", response_class=HTMLResponse)
def digest_page(request: Request, me: Who) -> Any:
    """S15: the latest weekly digest (SQL KPIs + the LLM's narrative, faithfulness-checked)."""
    with db() as conn:
        d = conn.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    return templates.TemplateResponse(request, "digest.html", {"me": me, "d": d})


@app.get("/jobs/{email_id}", response_class=HTMLResponse)
def job(request: Request, email_id: int, me: Who) -> Any:
    with db() as conn:
        mail = conn.execute(
            "SELECT id, from_name, from_addr, subject, sent_at, raw_sha256 FROM emails WHERE id = %s", (email_id,)
        ).fetchone()
        if not mail:
            raise HTTPException(404, "no such job")
        status = conn.execute("SELECT status, error FROM jobs WHERE email_id = %s", (email_id,)).fetchone()
        item = conn.execute(
            "SELECT payload, value_usd, needs_senior, updated_at FROM review_items WHERE email_id = %s", (email_id,)
        ).fetchone()
        ext = conn.execute("SELECT result FROM extractions WHERE email_id = %s", (email_id,)).fetchone()
        est = conn.execute(
            "SELECT part_code, result FROM estimates WHERE email_id = %s ORDER BY part_code", (email_id,)
        ).fetchall()
        supplier_rfqs = conn.execute(  # S17
            """SELECT id, code, vendor_name, vendor_email, subject, body, attachment, redaction, status, blocked, quote, approved_by,
                      due_at, need FROM supplier_rfqs WHERE email_id = %s ORDER BY code, id""", (email_id,)).fetchall()  # fmt: skip
        files = conn.execute(
            """SELECT id, filename, true_type, size FROM attachments
                                WHERE email_id = %s AND verdict = 'clean' ORDER BY id""",
            (email_id,),
        ).fetchall()
        decision = conn.execute(
            "SELECT action, decided_by, reason, final_text, decided_at FROM decisions WHERE email_id = %s", (email_id,)
        ).fetchone()
        acted = conn.execute("SELECT result, at FROM actions WHERE email_id = %s ORDER BY id", (email_id,)).fetchall()
        outcome = conn.execute(
            "SELECT quotation, state, outcome, lost_reason, changed_at FROM outcomes WHERE email_id = %s", (email_id,)
        ).fetchone()
        edits = conn.execute(
            "SELECT field, old_value, new_value, by_user, reason, at FROM feedback WHERE email_id = %s ORDER BY id", (email_id,)
        ).fetchall()
    msg = email.message_from_bytes(Store().get(f"raw/{mail['raw_sha256']}.eml"), policy=email_policy.default)
    for f in files:
        f["view"] = viewer(f["filename"], f["true_type"])
    return templates.TemplateResponse(request, "job.html", {
        "me": me, "mail": mail, "status": status, "item": item, "payload": (item or {}).get("payload") or {},
        "supplier_rfqs": supplier_rfqs,
        "whatif_session": uuid.uuid4().hex,  # S16: one what-if budget per page view (no inline script: the CSP forbids it)
        "extraction": (ext or {}).get("result") or {}, "estimates": [r["result"] for r in est], "files": files,
        "decision": decision, "edits": edits, "acted": acted, "outcome": outcome, "email_text": content.visible_text(msg),
    })  # fmt: skip


def viewer(filename: str, true_type: str | None) -> str:
    name, t = filename.lower(), (true_type or "").lower()
    if name.endswith(".pdf") or "pdf" in t:
        return "pdf"
    if name.endswith(".dxf"):
        return "dxf"
    if name.endswith((".png", ".jpg", ".jpeg")) or t.startswith("image/"):
        return "image"
    return "download"


@app.get("/jobs/{email_id}/files/{file_id}")
def file(email_id: int, file_id: int, me: Who, svg: bool = False) -> Response:
    """A drawing the security gate passed (files/ only, never quarantine/), and only one that belongs to this job."""
    with db() as conn:
        f = conn.execute("""SELECT filename, true_type, sha256 FROM attachments
                            WHERE id = %s AND email_id = %s AND verdict = 'clean'""", (file_id, email_id)).fetchone()  # fmt: skip
    if not f:
        raise HTTPException(404, "no such file")
    data = Store().get(f"files/{f['sha256']}")
    kind = viewer(f["filename"], f["true_type"])
    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", f["filename"])
    if svg and kind == "dxf":
        return Response(dxf_svg(data), media_type="image/svg+xml")
    media = {"pdf": "application/pdf", "image": f["true_type"] or "image/png"}.get(kind, "application/octet-stream")
    disposition = "inline" if kind in ("pdf", "image") else "attachment"
    return Response(data, media_type=media, headers={"Content-Disposition": f'{disposition}; filename="{safe_name}"'})


def dxf_svg(data: bytes) -> str:
    """The DXF drawn as SVG by ezdxf (shown in <img>, so nothing in it can run)."""
    from ezdxf import recover
    from ezdxf.addons.drawing import Frontend, RenderContext, layout, svg

    doc, _ = recover.read(io.BytesIO(data))
    backend = svg.SVGBackend()
    Frontend(RenderContext(doc), backend).draw_layout(doc.modelspace())
    return str(backend.get_string(layout.Page(0, 0, layout.Units.mm, margins=layout.Margins.all(5))))


# ---- the decision
@app.post("/jobs/{email_id}/decide", response_class=HTMLResponse)
async def decide(request: Request, email_id: int, me: Who, action: Annotated[str, Form()],
                 reason: Annotated[str, Form()] = "", text: Annotated[str, Form()] = "") -> Any:  # fmt: skip
    if request.headers.get("hx-request") != "true":  # CSRF: a cross-site form can't set this header
        raise HTTPException(400, "send decisions from the review screen")
    reason = reason.strip()
    if action not in ACTIONS:
        return result(request, 400, [f"unknown action {action!r}"])
    if action != "approve" and not reason:
        return result(request, 400, ["a reason is required to edit, re-cost, clarify or reject"])
    with db() as conn:
        item = conn.execute("SELECT payload FROM review_items WHERE email_id = %s", (email_id,)).fetchone()
        ext = conn.execute("SELECT result FROM extractions WHERE email_id = %s", (email_id,)).fetchone()
    if not item:
        return result(request, 404, ["this job is not waiting for review"])
    decision: dict[str, Any] = {"action": action, "by": me.username, "name": me.name, "roles": me.roles, "reason": reason,
                                "via": "review-ui"}  # fmt: skip
    if denied := opa.review_denied(S.opa_url, decision, item["payload"]["risk"], email_id):
        audit("decision blocked", email_id=email_id, action=action, by=me.username, roles=me.roles, why=denied)
        return result(request, 403, denied)
    if action == "edit":
        if not text.strip():
            return result(request, 400, ["write the email you want sent"])
        decision["edits"] = {"customer_language_text": text}
    if action == "recost":
        changed = changed_fields(await request.form(), (ext or {}).get("result") or {})
        if not changed:
            return result(request, 400, ["change at least one field to re-cost"])
        decision["edits"] = {"fields": changed}
    draft = item["payload"].get("draft") or {}
    if action == "clarify":
        if text.strip() and text.strip() != (draft.get("customer_language_text") or "").strip():
            decision["edits"] = {"customer_language_text": text}
        elif draft.get("mode") != "clarify":
            return result(request, 400, ["write your question to the customer in the email box first"])
    if action in ("approve", "edit", "clarify"):
        # S8: sign what the person approved. The write tools send/write only content whose hashes match this token.
        body = (decision.get("edits") or {}).get("customer_language_text") or draft.get("customer_language_text") or ""
        if not body.strip():
            return result(request, 400, ["there is no email to send: write it with Edit & approve"])
        with db() as conn:
            mail = conn.execute("SELECT from_addr, subject FROM emails WHERE id = %s", (email_id,)).fetchone()
            est = conn.execute("SELECT result FROM estimates WHERE email_id = %s", (email_id,)).fetchall()
        if not mail:
            return result(request, 404, ["no such email"])
        risk = item["payload"]["risk"]
        decision["approval"] = approval.sign(
            S.approval_key.get_secret_value(), email_id=email_id, action=action, by=me.username, roles=me.roles,
            needs_senior=bool(risk.get("needs_senior")), senior=risk.get("senior") or [],
            text_sha=approval.text_sha256(mail["from_addr"], approval.reply_subject(draft.get("subject"), mail["subject"]), body),
            prices_sha=approval.prices_sha256(approval.price_lines([r["result"] for r in est])),
        )  # fmt: skip
    with db() as conn:
        n = conn.execute("""UPDATE jobs SET status = 'decided', decision = %s
                            WHERE email_id = %s AND status = 'awaiting_review'""", (Json(decision), email_id)).rowcount  # fmt: skip
    if not n:
        return result(request, 409, ["someone already decided this job (reload the page)"])
    audit("decision queued", email_id=email_id, action=action, by=me.username, roles=me.roles)
    return templates.TemplateResponse(request, "_status.html", {"email_id": email_id, "status": "decided", "decision": decision})


EXTRA_FIELDS = {"price_adjust_pct": 0}  # S18: a reviewer's price override per part (%, with the re-cost's reason)


def changed_fields(form: Any, extraction: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Form inputs named `f|<part>|<field>` -> the values that differ from what was read (typed like the original)."""
    current = {p["part_code"]: {k: f["value"] for k, f in p["fields"].items()} for p in extraction.get("parts", [])}
    out: dict[str, dict[str, Any]] = {}
    for key, raw in form.multi_items():
        if not key.startswith("f|"):
            continue
        _, code, name = key.split("|", 2)
        if code not in current or (name not in current[code] and name not in EXTRA_FIELDS):
            continue
        old = current[code].get(name, EXTRA_FIELDS.get(name))
        try:
            new = raw if isinstance(old, str) else json.loads(raw)
        except ValueError:
            new = raw
        if new != old:
            out.setdefault(code, {})[name] = new
    return out


def result(request: Request, code: int, messages: list[str], ok: bool = False) -> Any:
    return templates.TemplateResponse(request, "_result.html", {"messages": messages, "ok": ok}, status_code=code)


@app.get("/jobs/{email_id}/status", response_class=HTMLResponse)
def status(request: Request, email_id: int, me: Who) -> Any:
    """Polled by the page after a decision until the worker has applied it."""
    with db() as conn:
        row = conn.execute("SELECT status, error, decision FROM jobs WHERE email_id = %s", (email_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such job")
    return templates.TemplateResponse(request, "_status.html", {"email_id": email_id, **row})
