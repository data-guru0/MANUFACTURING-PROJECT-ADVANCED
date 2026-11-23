"""The quote graph (S6): one LangGraph StateGraph per job, checkpointed in Postgres after every node.

  security_gate -> triage --not_rfq--> END
                     \\--rfq--> extract -> reconcile -> retrieve --Send(part)--> cost_part (fan-out, joined)
                                                          -> risk_policy (OPA) --quote|clarify|escalate--> draft_reply
                                                          -> human_review (interrupt) -> act -> learn -> END
                                                   human_review --recost (S7: reviewer corrected fields)--> reconcile
Guards: per-node timeout and retries (policy.yaml graph:), the recursion limit, a per-job $ budget (OPA escalates),
idempotency (each node reuses its stored result: S3-S5 tables keyed by email), and the checkpoint itself: a killed
worker resumes at the next node, never re-running a finished one. Every step is logged in graph_events.
"""

import copy
import dataclasses
import email
import re
import uuid
from datetime import date
from email import policy as email_policy
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, Send, interrupt
from psycopg.types.json import Json

from apex_agent import opa
from apex_agent import replies as reply_mod
from apex_agent import suppliers as sup
from apex_agent.actions import act as act_effects
from apex_agent.costing import capability, capacity, dfm, priority, win_model
from apex_agent.costing import repeat as repeats
from apex_agent.costing.engine import estimate, part_from_spec
from apex_agent.costing.quote import analog_check, build_context, fetch_facts, quote_part
from apex_agent.costing.standards import Part
from apex_agent.graph import draft as drafting
from apex_agent.graph.state import JobState
from apex_agent.knowledge import db
from apex_agent.llm.client import NeedsHuman
from apex_agent.security import content
from apex_agent.security.pii import log

EXPORT_CONTROL = re.compile(r"\b(ITAR|EAR99|export[- ]controll?ed|export control|dual[- ]use)\b", re.I)  # S9, code only
ACTIONS = {"approve", "edit", "clarify", "reject", "recost"}  # recost: S7, back to reconcile with edited fields


class QuoteGraph:
    def __init__(self, w: Any, checkpoint_conn: Any) -> None:
        """`w`: the worker (its gate, triage, extractor, store, LLM clients and DB connection)."""
        self.w = w
        cfg = opa.decide(w.opa_url, "policy/graph", {}) or {}
        self.cfg = {"job_budget_usd": 0.25, "step_timeout_s": 900, "max_attempts": 2, "recursion_limit": 60, **cfg}
        self.saver = PostgresSaver(checkpoint_conn)
        self.saver.setup()
        retry = RetryPolicy(max_attempts=int(self.cfg["max_attempts"]), retry_on=lambda e: not isinstance(e, NeedsHuman))
        g = StateGraph(JobState)
        for name in ("security_gate", "triage", "repeat", "extract", "reconcile", "retrieve", "capability", "cost_part", "risk_policy", "draft_reply",
                     "human_review", "act", "learn", "reply_router", "follow_up", "suppliers", "supplier_reply"):  # fmt: skip
            g.add_node(name, self._logged(name), retry_policy=retry)
        # S15: a due follow-up re-enters a finished thread here (worker -> followup()), straight to the reminder
        g.add_conditional_edges(
            START, lambda s: "follow_up" if s.get("followup_due") else "security_gate", ["follow_up", "security_gate"]
        )
        g.add_conditional_edges("follow_up", lambda s: "human_review" if (s.get("risk") or {}).get("route") == "followup" and s.get("followup_drafted")
                                else END, ["human_review", END])  # fmt: skip
        g.add_conditional_edges("security_gate", lambda s: END if s.get("status") == "manual" else "triage", ["triage", END])
        g.add_conditional_edges("triage", lambda s: {"rfq": "extract", "rfq_followup": "reply_router", "supplier_reply": "supplier_reply"}.get(s["gate"]["triage"], END),
                                ["extract", "reply_router", "supplier_reply", END])  # fmt: skip
        # S15: a reply to a quote: accept / reject / negotiate / question go to a person (reject is booked in Odoo first and
        # ends), a revision goes through the quoting pipeline, anything else ends
        g.add_conditional_edges("reply_router", self._after_reply, ["human_review", "extract", END])
        g.add_edge("extract", "repeat")  # S12 after extraction: it names each part (the email + the drawing), so a scan's
        # OCR-misread rev is corrected and a part asked for by number alone (no drawing) is matched too
        g.add_edge("repeat", "reconcile")  # repeat / revision of an archived part? (code; the LLM only words a diff)
        g.add_edge("reconcile", "retrieve")
        g.add_edge("retrieve", "capability")  # S11: can Apex make it? no-bid parts are not costed
        g.add_conditional_edges("capability", self._fan_out, ["cost_part", "risk_policy"])
        g.add_edge("cost_part", "suppliers")  # S17: stale supplier prices / short material -> supplier RFQ drafts
        g.add_edge("suppliers", "risk_policy")
        g.add_edge("supplier_reply", END)  # S17: a supplier's answer to our RFQ
        g.add_edge("risk_policy", "draft_reply")
        g.add_edge("draft_reply", "human_review")
        g.add_conditional_edges(
            "human_review", lambda s: "reconcile" if s["review"]["action"] == "recost" else "act", ["reconcile", "act"]
        )
        g.add_edge("act", "learn")
        g.add_edge("learn", END)
        self.graph = g.compile(checkpointer=self.saver)
        # LangGraph's per-node timeout needs async nodes (sync code can't be cancelled safely); a sync graph gets a
        # per-step deadline instead: a step (one node, or the whole fan-out) longer than this raises and is retried.
        self.graph.step_timeout = float(self.cfg["step_timeout_s"])

    @staticmethod
    def _after_reply(s: JobState) -> str:
        route = (s.get("reply") or {}).get("route")
        if route == "revision":
            return "extract"
        return (
            "human_review" if s.get("draft") is not None and (s.get("risk") or {}).get("route", "").startswith("reply_") else END
        )

    # ---- running
    def followup(self, email_id: int) -> str:
        """S15: the job's follow-up is due: re-enter its (finished) thread at `follow_up`."""
        # the ids too: the thread's checkpoint may have been cleaned up since (S9 retention, a re-run)
        self.graph.invoke({"followup_due": True, "email_id": email_id, "trace_id": uuid.uuid4().hex}, self.config(email_id))
        return self.status(email_id)

    def config(self, email_id: int) -> dict[str, Any]:
        return {"configurable": {"thread_id": f"email-{email_id}"}, "recursion_limit": int(self.cfg["recursion_limit"])}

    def run(self, email_id: int) -> str:
        """Start the job, or resume it from its last checkpoint (after a crash). Returns the job's status."""
        cfg = self.config(email_id)
        snap = self.graph.get_state(cfg)
        if snap.next and snap.interrupts:
            return "awaiting_review"  # waiting for a person; nothing to do until Command(resume=...)
        inp = None if snap.values else {"email_id": email_id, "trace_id": uuid.uuid4().hex, "spent_usd": 0.0, "estimates": []}
        if snap.values:
            log("graph resume", email_id=email_id, next=",".join(snap.next))
        self.graph.invoke(inp, cfg)
        return self.status(email_id)

    def resume(self, email_id: int, decision: dict[str, Any]) -> str:
        if decision.get("action") not in ACTIONS:
            raise ValueError(f"action must be one of {sorted(ACTIONS)}")
        if decision["action"] != "approve" and not decision.get("reason"):
            raise ValueError("a reason is required to edit, re-cost, clarify or reject")
        snap = self.graph.get_state(self.config(email_id))
        if not snap.interrupts:
            raise ValueError(f"email {email_id} is not waiting for review")
        # S7: every caller (review screen, CLI) is checked HERE, against the job's own risk in the checkpoint.
        if denied := opa.review_denied(self.w.opa_url, decision, snap.interrupts[0].value["risk"], email_id):
            raise PermissionError("; ".join(denied))
        self.graph.invoke(Command(resume=decision), self.config(email_id))
        status = self.status(email_id)
        self.w.set_status(email_id, status)  # the jobs table mirrors the graph (the queue, Grafana, the review screen)
        return status

    def status(self, email_id: int) -> str:
        snap = self.graph.get_state(self.config(email_id))
        if snap.next and snap.interrupts:
            return "awaiting_review"
        return str(snap.values.get("status") or "done")

    # ---- plumbing
    def _logged(self, name: str) -> Any:
        def node(state: Any) -> Any:
            email_id = state["email_id"]
            before = self.w.llm_spent()
            out = getattr(self, name)(state)
            spent = round(self.w.llm_spent() - before, 6)
            if isinstance(out, dict):
                out = {**out, "spent_usd": spent}
            with db.connect(self.w.schema) as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO graph_events (email_id, node, spent_usd) VALUES (%s, %s, %s)", (email_id, name, spent))
                conn.commit()
            return out

        return node

    def _fan_out(self, s: JobState) -> list[Send] | str:
        if not s.get("ready"):
            return "risk_policy"
        return [Send("cost_part", {**s, "item": item}) for item in s["ready"]]

    # ---- nodes
    def _gate_row(self, email_id: int) -> Any:
        with self.w.conn.cursor() as cur:
            cur.execute(
                "SELECT triage, red_banner, reasons, sender->>'known_customer' FROM gate_results WHERE email_id = %s", (email_id,)
            )
            return cur.fetchone()

    def security_gate(self, s: JobState) -> dict[str, Any]:
        # S9 data governance: export-controlled RFQs may be kept off external LLMs. A code-only check, BEFORE the gate's
        # first model call; OPA (apex/data) decides from policy.yaml -> data_governance.
        mail = self._email_text(s["email_id"], with_subject=True)
        export = bool(EXPORT_CONTROL.search(mail))
        if export and not opa.allowed(
            self.w.opa_url, "apex/data/external_llm_allowed", {"export_controlled": True, "email_id": s["email_id"]}
        ):
            return {
                "status": "manual",
                "manual_reason": "export-controlled: kept off external LLMs by policy; a person quotes it the legacy way",
            }
        if not self._gate_row(s["email_id"]):  # idempotent: a stored result (an earlier run) is reused
            self.w.run_gate(s["email_id"], s["trace_id"])
        return {}

    def triage(self, s: JobState) -> dict[str, Any]:
        with self.w.conn.cursor() as cur:  # S17: an answer to OUR supplier RFQ is known from its headers: no LLM call
            cur.execute(
                "SELECT 1 FROM emails e JOIN supplier_rfqs r ON r.message_id = e.in_reply_to WHERE e.id = %s", (s["email_id"],)
            )
            ours = cur.fetchone() is not None
            if ours:
                cur.execute("""UPDATE gate_results SET triage = 'supplier_reply', triage_confidence = 1,
                               triage_reason = 'In-Reply-To one of our supplier RFQs (headers, no LLM)'
                               WHERE email_id = %s AND triage IS NULL""", (s["email_id"],))  # fmt: skip
        self.w.conn.commit()
        if self._gate_row(s["email_id"])[0] is None:
            self.w.run_triage(s["email_id"], s["trace_id"])
        cat, red, reasons, customer = self._gate_row(s["email_id"])
        if ours:
            cat = "supplier_reply"
        return {"gate": {"triage": cat, "red_banner": red, "reasons": reasons, "customer": customer},
                "status": "triaged" if cat != "rfq" else "running"}  # fmt: skip

    def repeat(self, s: JobState) -> dict[str, Any]:
        """S12 (W10): each drawing against Apex's archive, same customer only (costing/repeat.py). A revision is diffed
        against the archived drawing (erp-mcp-read, OPA) and the LLM summarises the computed diff. Stored per email:
        a re-run never repeats the LLM call."""
        e = s["email_id"]
        with self.w.conn.cursor() as cur:
            cur.execute("SELECT result FROM repeat_matches WHERE email_id = %s", (e,))
            if row := cur.fetchone():
                return {"repeat": row[0]}
            cur.execute("""SELECT filename, sha256 FROM attachments WHERE email_id = %s AND verdict = 'clean'
                           AND true_type <> 'application/zip' ORDER BY id""", (e,))  # fmt: skip
            files = [(name, self.w.store.get(f"files/{sha}")) for name, sha in cur.fetchall()]
        with db.connect(self.w.schema) as conn:
            found = repeats.detect(conn, s["gate"].get("customer"), files, parts=s["extraction"]["parts"])
            for key, m in found.items():
                if m["kind"] != "revision" or not m["_group"]["files"]:  # no drawing sent: nothing to diff
                    continue
                old = repeats.approved_spec(conn, m["of"])[1]
                m["diff"] = repeats.diff(repeats.fetch_archive(m["of"]), old, m["_group"])
                try:
                    m["summary"] = drafting.summarize_revision(self.w.extractor_llm, m["of"], key, m["diff"], s["trace_id"])
                except Exception as ex:  # noqa: BLE001  the diff itself is computed; only its wording is missing
                    m["summary"] = None
                    m["summary_error"] = type(ex).__name__
        result = {k: repeats.public(m) for k, m in found.items()}
        if any(m.get("summary_error") for m in result.values()):
            return {"repeat": result}  # not stored: a later re-run tries the summary again (e.g. after the quota refills)
        with self.w.conn.cursor() as cur:
            cur.execute("""INSERT INTO repeat_matches (email_id, result) VALUES (%s, %s)
                           ON CONFLICT (email_id) DO UPDATE SET result = EXCLUDED.result""", (e, Json(result)))  # fmt: skip
        self.w.conn.commit()
        return {"repeat": result}

    def extract(self, s: JobState) -> dict[str, Any]:
        with self.w.conn.cursor() as cur:
            cur.execute("SELECT result FROM extractions WHERE email_id = %s", (s["email_id"],))
            row = cur.fetchone()
        if not row:
            self.w.extract(s["email_id"], s["trace_id"])
            return self.extract(s)
        return {"extraction": row[0]}

    def reconcile(self, s: JobState) -> dict[str, Any]:
        """Which parts can be costed: the extraction, completed from Apex's own drawing of a repeat part (part_specs)."""
        ready, not_costed = [], []
        for p in s["extraction"]["parts"]:
            fields = {k: f["value"] for k, f in p["fields"].items()}
            fields.update((s.get("overrides") or {}).get(p["part_code"], {}))  # S7: the reviewer's values win
            family = fields.get("family")
            rep = (s.get("repeat") or {}).get(p["part_code"]) or {}
            if rep.get("kind") == "repeat":  # S12: the APPROVED spec of the archived part is reused, not the re-read drawing
                own_family, own = repeats.approved_spec(self.w.conn, rep["of"])
                if own:
                    family, fields = own_family, {**own, **(s.get("overrides") or {}).get(p["part_code"], {})}
            elif not fields.get("envelope_mm"):
                with self.w.conn.cursor() as cur:
                    cur.execute("SELECT family, fields FROM part_specs WHERE code = %s", (p["part_code"],))
                    if own := cur.fetchone():
                        family = family or own[0]
                        fields = {
                            **{k: f["value"] for k, f in own[1].items()},
                            **{k: v for k, v in fields.items() if v is not None},
                        }
            part = part_from_spec(fields, family)
            if part and p["quantities"]:
                ready.append(
                    {
                        "code": p["part_code"],
                        "part": part.__dict__,
                        "quantities": p["quantities"],
                        "thread_size": fields.get("thread_size"),  # S13: DFM (non-standard threads)
                        "price_adjust": {k: fields.get(f"price_adjust_{k}") for k in ("pct", "by", "reason")}
                        if fields.get("price_adjust_pct")
                        else None,  # S18: a reviewer's override
                        **({"repeat": rep} if rep.get("kind") in ("repeat", "revision") else {}),
                    }
                )
            else:
                not_costed.append(f"{p['part_code']}: " + ("no quantity" if part else "family, size or material unknown"))
        return {"ready": ready, "not_costed": not_costed}

    def retrieve(self, s: JobState) -> dict[str, Any]:
        """Live facts for costing, once per job: rates, prices, customer tier, open workload (erp-mcp-read via OPA)."""
        if not s.get("ready"):
            return {"facts": {}}
        rules = opa.decide(self.w.opa_url, "policy/dfm", {}) or {}  # S13: the DFM rules (and their alternative materials)
        alts = rules.get("material_alternatives") or {}
        mats = sorted({r["part"]["material"] for r in s["ready"]} | {alts[r["part"]["material"]]["to"] for r in s["ready"]
                                                                      if r["part"]["material"] in alts})  # fmt: skip
        osps = sorted({o for r in s["ready"] for o in r["part"]["outside"]})
        f = fetch_facts(self.w.opa_url, mats, osps, s["gate"].get("customer"))
        # S14 (W7): the open quotes' expected load (minutes x P(win)) and the heatmap snapshot, once per job
        with db.connect(self.w.schema) as conn:
            quotes = capacity.open_quotes(conn, s["email_id"])
            snap = capacity.weekly(f["load"], quotes, date.today())
            capacity.store_snapshot(conn, snap)
        esc = opa.decide(self.w.opa_url, "policy/escalation", {}) or {}
        return {"facts": {**f, "dfm": rules, "escalation": esc, "expected": capacity.expected(quotes), "capacity": snap}}

    def capability(self, s: JobState) -> dict[str, Any]:
        """S11 (W11): every part against the machines, materials, processes and certificates (costing/capability.py, code).
        A no-bid part is not costed; if every part is a no-bid, the route is `no_bid` (a decline draft, for approval)."""
        if not s.get("ready"):
            return {"capability": {}}
        f = s["facts"]
        held = (opa.decide(self.w.opa_url, "policy/capability", {}) or {}).get("certificates_held", [])
        certs_text = str(((s["extraction"]["requirements"].get("certificates") or {}).get("value")) or "")
        certs = [c.strip() for c in re.split(r"[;,/]|\band\b", certs_text) if c.strip()]
        verdicts, ready, not_costed = {}, [], list(s.get("not_costed") or [])
        for item in s["ready"]:
            part = Part(**item["part"])
            info = (f.get("mats") or {}).get(part.material) or [{}]
            v = capability.check(part, f.get("wcs") or [], material_known=bool(info[0].get("found", True)),
                                 outside_priced={o: bool((f.get("osps") or {}).get(o)) for o in part.outside},
                                 certificates=certs, held=held)  # fmt: skip
            verdicts[item["code"]] = v
            if v["decision"] == "no_bid":
                not_costed.append(f"{item['code']}: we can't make it ({'; '.join(v['reasons'])})")
            else:
                ready.append(item)
        return {"capability": verdicts, "ready": ready, "not_costed": not_costed}

    def cost_part(self, s: dict[str, Any]) -> dict[str, Any]:
        """One part (a fan-out branch): own DB connection, the deterministic S5 estimate, stored by (email, part)."""
        item, req = s["item"], s["extraction"]["requirements"]

        def rv(name: str) -> Any:
            return (req.get(name) or {}).get("value")

        part = Part(**item["part"])
        on = date.today()
        ctx = build_context(s["facts"], on, self.w.cycle_model())
        if adj := item.get("price_adjust"):  # S18: the reviewer's override, as an attributed rule (code applies it)
            ctx = dataclasses.replace(ctx, rules=[*ctx.rules, {"id": "MANUAL", "effect": {"type": "add_percent", "value": float(adj["pct"])},
                                                               "approved_by": adj["by"], "summary": f"reviewer override: {adj['reason']}",
                                                               "when": {"materials": [part.material]}}])  # fmt: skip
        certs = str(rv("certificates") or "")
        docs = {"material_cert_3_1": "3.1" in certs, "fai_report": bool(rv("fai")), "ppap": bool(rv("ppap"))}
        # S14: the delivery date books the open quotes' expected load on top of the confirmed work (policy weight)
        load, esc = s["facts"]["load"], s["facts"].get("escalation") or {}
        weight, exp = float(esc.get("expected_load_weight", 0.0)), s["facts"].get("expected") or {}
        loaded = {**load, "open_minutes": {wc: load["open_minutes"].get(wc, 0.0) + weight * exp.get(wc, 0.0)
                                           for wc in set(load["open_minutes"]) | set(exp)}}  # fmt: skip
        with db.connect(self.w.schema) as conn:
            out = quote_part(
                conn, part, item["quantities"], ctx, loaded, on, rv("requested_date"), docs, bool(rv("co2_requested"))
            )
            if weight and exp:  # the same lot against the confirmed work only: what the expected wins cost in days
                alone = quote_part(conn, part, item["quantities"], ctx, load, on, None, docs, False)["delivery"]["committed"]
                out["delivery"]["committed_confirmed_only"] = alone
                out["delivery"]["expected_load_days"] = (
                    date.fromisoformat(out["delivery"]["committed"]) - date.fromisoformat(alone)
                ).days
            out["flags"] += capacity.overcommit(s["facts"].get("capacity") or {"rows": {}}, out["delivery"]["steps"])
            out["analogs"] = analog_check(conn, part, out["breaks"][0]["unit_price"], ctx.policy)
            if out["analogs"]["flag"]:
                out["flags"].append(out["analogs"]["flag"])
            out["code"] = item["code"]
            if adj := item.get("price_adjust"):
                out["manual_override"] = adj
            # S13 (W12): DFM, each suggestion priced by the engine with one feature changed; optional lines only
            b0 = out["breaks"][0]
            out["dfm"] = dfm.suggest(part, int(b0["qty"]), ctx, docs, item.get("thread_size"), s["facts"].get("dfm") or {},
                                     b0["unit_price"])  # fmt: skip
            if rep := item.get("repeat"):
                out["repeat"] = self._reprice(conn, rep, part, out, ctx, docs)
            # S14 (W6): P(win) at the engine's price and the margin curve (advisory; OPA holds the floor and ceiling)
            if wm := win_model.load(self.w.store):
                days = self._response_days(s["email_id"])
                cv = win_model.curve(wm, b0["unit_cost"], int(b0["qty"]), ctx.customer.get("tier"), days,
                                     float(esc.get("margin_floor_pct", 12)), float(esc.get("margin_ceiling_pct", 45)), b0["unit_price"])  # fmt: skip
                out["win"] = {"p_win": cv["quoted"]["p_win"], "validated": wm["validated"], "model": wm.get("version"),
                              "brier": (wm.get("card") or {}).get("brier"), "curve": cv}  # fmt: skip
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO estimates (email_id, part_code, result) VALUES (%s, %s, %s)
                               ON CONFLICT (email_id, part_code) DO UPDATE SET result = EXCLUDED.result, created_at = now()""",
                            (s["email_id"], item["code"], Json(out)))  # fmt: skip
            conn.commit()
        return {"estimates": [out]}

    def _response_days(self, email_id: int) -> float:
        """Days from the RFQ arriving to now (the reply goes out after review; the model's feature)."""
        with self.w.conn.cursor() as cur:
            cur.execute(
                "SELECT extract(epoch FROM now() - coalesce(sent_at, ingested_at)) / 86400 FROM emails WHERE id = %s", (email_id,)
            )
            row = cur.fetchone()
        return max(float(row[0]), 0.0) if row and row[0] is not None else 0.0

    @staticmethod
    def _reprice(
        conn: Any, rep: dict[str, Any], part: Part, out: dict[str, Any], ctx: Any, docs: dict[str, bool]
    ) -> dict[str, Any]:
        """S12: old vs new price, with the reasons. A revision: the cost of the CHANGE is the engine's estimate of the new
        spec minus the old spec, both at today's rates and prices (line by line); the rest of the part is unchanged."""
        b = out["breaks"][0]
        with conn.cursor() as cur:
            cur.execute("SELECT last_price, quotes, won FROM parts WHERE code = %s", (rep["of"],))
            last, quotes, won = cur.fetchone() or (None, 0, 0)
        r: dict[str, Any] = {"kind": rep["kind"], "of": rep["of"], "method": rep["method"], "why": rep["why"],
                             "old_unit_price": float(last) if last is not None else None, "new_unit_price": b["unit_price"],
                             "history": f"quoted {quotes or 0}x, won {won or 0}x"}  # fmt: skip
        if last:
            r["change_pct"] = round(100 * (b["unit_price"] - float(last)) / float(last), 1)
        fam_old, f_old = repeats.approved_spec(conn, rep["of"])
        reasons = ["priced by the engine at today's machine rates, material prices and this customer's tier"]
        with conn.cursor() as cur:  # the last quote's quantity (Odoo sale.order.line in the read model)
            cur.execute("""SELECT (data->>'product_uom_qty')::numeric FROM odoo_records WHERE model = 'sale.order.line'
                           AND data->'product_id'->>1 LIKE %s ORDER BY (data->'order_id'->>0)::int DESC LIMIT 1""",
                        (f"[{rep['of']}]%",))  # fmt: skip
            old_qty = int(row[0]) if (row := cur.fetchone()) and row[0] else None
        r["old_qty"], r["new_qty"] = old_qty, b["qty"]
        if old_qty and old_qty != b["qty"] and last:  # like for like: today's price at the OLD quantity
            r["today_at_old_qty"] = estimate(part, old_qty, ctx, docs).unit_price
            reasons.append(f"quantity {old_qty} -> {b['qty']}: at the old quantity today's price is "
                           f"${r['today_at_old_qty']:.2f} ({100 * (r['today_at_old_qty'] - float(last)) / float(last):+.1f} % vs ${float(last):.2f})")  # fmt: skip
        if rep["kind"] == "repeat":
            reasons.insert(0, f"the approved spec of {rep['of']} is reused (same customer; matched by {rep['method']})")
        elif f_old and (old := part_from_spec(f_old, fam_old)):
            try:
                e_old = estimate(old, b["qty"], ctx, docs)
            except KeyError as ex:  # e.g. the old rev's material is not among today's fetched prices
                reasons.append(f"the change could not be costed separately ({ex} not in today's facts)")
            else:
                new_lines = {ln["name"]: ln["amount"] for ln in b["lines"]}
                old_lines = {ln.name: ln.amount for ln in e_old.lines}
                delta = {n: round(new_lines.get(n, 0) - old_lines.get(n, 0), 2) for n in sorted(set(new_lines) | set(old_lines))}
                r["change_cost"] = {"per_unit": round(b["unit_price"] - e_old.unit_price, 2),
                                    "lines": {n: d for n, d in delta.items() if abs(d) >= 0.01},
                                    "old_spec_today": e_old.unit_price}  # fmt: skip
                if last:
                    r["old_plus_change"] = round(float(last) + r["change_cost"]["per_unit"], 2)
                reasons.insert(
                    0, f"revision of {rep['of']}: the change costs {r['change_cost']['per_unit']:+.2f} $/unit at today's costs"
                )
            r["changes"] = (rep.get("diff") or {}).get("fields", [])
            r["summary"] = rep.get("summary")
            r["pixels"] = {k: v for k, v in (((rep.get("diff") or {}).get("pdf") or {}).get("pixels") or {}).items()}
        r["reasons"] = reasons
        return r

    def risk_policy(self, s: JobState) -> dict[str, Any]:
        est = s.get("estimates") or []
        caps = s.get("capability") or {}
        value = sum(e["breaks"][0]["unit_price"] * e["breaks"][0]["qty"] for e in est)
        x = s.get("extraction") or {}

        def margins_of(n: int | None) -> list[float]:  # margin % per price break (n=1: the order line; None: every break)
            return [100 * m / (b["unit_price"] - m) for e in est for b in e["breaks"][:n]
                    for m in [sum(ln["amount"] for ln in b["lines"] if ln["name"] == "margin")] if b["unit_price"] > m]  # fmt: skip

        margins, every = margins_of(1), margins_of(None)
        inp = {"red_banner": s["gate"]["red_banner"], "spent_usd": s.get("spent_usd", 0.0), "estimates": [e["code"] for e in est],
               "clarification": x.get("clarification", []), "extraction_route": x.get("route"), "value_usd": round(value, 2),
               # S9 escalation reasons (escalation.rego decides what each one means)
               "margin_pct": round(min(margins), 1) if margins else None, "margin_max_pct": round(max(every), 1) if every else None,
               "new_customer": not s["gate"].get("customer"),
               "low_confidence": len(x.get("needs_human") or []), "email_id": s["email_id"],
               "infeasible_date": any("is not feasible" in f for e in est for f in e["flags"]),
               "no_bid": bool(caps) and all(v["decision"] == "no_bid" for v in caps.values())}  # fmt: skip
        d = opa.decide(self.w.opa_url, "apex/risk/decision", inp)
        if not isinstance(d, dict):  # fail closed
            d = {
                "route": "escalate",
                "escalate": ["policy service (OPA) unavailable"],
                "senior": [],
                "needs_senior": True,
                "check": [],
            }
        conditions = [f"{code}: {c}" for code, v in caps.items() for c in v["conditions"]]
        with db.connect(self.w.schema) as conn:
            prio = priority.score(
                conn, est, value, x.get("requirements") or {}, bool(inp["infeasible_date"]), bool(inp["no_bid"])
            )
        return {"risk": {**d, "value_usd": round(value, 2), "flags": [f for e in est for f in e["flags"]],
                         "not_costed": s.get("not_costed", []), "needs_human": x.get("needs_human", []),
                         "conditions": conditions, "priority": prio,
                         "repeats": {e["code"]: e["repeat"] for e in est if e.get("repeat")},
                         "suppliers": s.get("supplier_rfqs") or [],
                         "repeat_hints": {k: v for k, v in (s.get("repeat") or {}).items()
                                          if v.get("kind") == "new" and (v.get("why") or v.get("similar"))}}}  # fmt: skip

    def draft_reply(self, s: JobState) -> dict[str, Any]:
        r = s["risk"]
        if r["route"] == "escalate" and "LLM budget" in " ".join(r["escalate"]):
            return {"draft": {"faithful": False, "skipped": "the job's LLM budget is spent: a person writes the reply"}}
        x = s["extraction"]
        req = x["requirements"]
        mode = {"clarify": "clarify", "no_bid": "decline"}.get(r["route"], "quote")
        facts: dict[str, Any] = {"mode": mode, "reply_language": req.get("reply_language", "en"), "currency": "USD"}
        if mode == "clarify":
            facts["questions"] = x["clarification"]
        elif mode == "decline":  # S11: politely, with the reasons; no price
            facts["reasons"] = [r for v in (s.get("capability") or {}).values() for r in v["reasons"]]
        else:
            facts["parts"] = [{"part": e["code"], "price_breaks": [{"quantity": b["qty"], "unit_price": b["unit_price"]} for b in e["breaks"]],
                               "committed_delivery_date": e["delivery"]["committed"], "valid_until": e["terms"]["validity_date"],
                               "price_protection": e["terms"]["clause"],
                               **({"optional_alternatives": [
                                   {"change": f"{d['field']} {d['old']} -> {d['new']}", "quantity": d["qty"],
                                    "unit_price": d["alt_unit_price"], "saving_pct": d["saving_pct"], "why": d["why"]}
                                   for d in (e.get("dfm") or {}).get("suggestions", []) if d["offered"]]}
                                  if any(d["offered"] for d in (e.get("dfm") or {}).get("suggestions", [])) else {}),
                               **({"conditions": (s.get("capability") or {}).get(e["code"], {}).get("conditions")}
                                  if (s.get("capability") or {}).get(e["code"], {}).get("conditions") else {}),
                               **({"co2_kg_per_part": e["co2"]["per_part_kg"], "co2_method": "estimate, not a certified footprint"}
                                  if e["co2"]["include_in_quote"] else {})} for e in s["estimates"]]  # fmt: skip
            facts["not_quoted"] = s.get("not_costed", [])
        raw = self._email_text(s["email_id"])
        out = drafting.draft(self.w.drafter_llm, facts, raw, s["trace_id"])
        return {"draft": {**out, "mode": mode, "facts": facts}}

    def human_review(self, s: JobState) -> dict[str, Any]:
        """Nothing goes out without a person: pause here until Command(resume={action, by, reason, edits}).
        The payload is also stored in review_items, which the review screen (S7) reads; this node runs again from the
        top on every resume, so the row is simply rewritten."""
        e, r = s["email_id"], s["risk"]
        payload = {"email_id": e, "route": r["route"], "risk": r, "trace_id": s.get("trace_id"),
                   "draft": {k: v for k, v in s["draft"].items() if k != "facts"}}  # fmt: skip
        with self.w.conn.cursor() as cur:
            cur.execute("""INSERT INTO review_items (email_id, route, value_usd, needs_senior, payload) VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (email_id) DO UPDATE SET route = EXCLUDED.route, value_usd = EXCLUDED.value_usd,
                             needs_senior = EXCLUDED.needs_senior, payload = EXCLUDED.payload, updated_at = now()""",
                        (e, r["route"], r.get("value_usd", 0.0), bool(r.get("needs_senior")), Json(payload)))  # fmt: skip
        self.w.conn.commit()
        # (no set_status here: this line would run again at the START of every resume and show the job as waiting while
        # it is still being re-costed; the worker sets the status once run()/resume() returns)
        decision = interrupt(payload)
        if decision["action"] != "recost":
            return {"review": decision}
        # A re-cost: the reviewer corrected what was read. Store each change as a diff, then cost again (code, not the LLM).
        # ponytail: the diff is written once per resume; a crash between this write and the checkpoint repeats the row.
        overrides = copy.deepcopy(s.get("overrides") or {})
        current = {p["part_code"]: {k: f["value"] for k, f in p["fields"].items()} for p in s["extraction"]["parts"]}
        with self.w.conn.cursor() as cur:
            for code, changed in ((decision.get("edits") or {}).get("fields") or {}).items():
                for name, new in changed.items():
                    old = overrides.get(code, {}).get(name, current.get(code, {}).get(name))
                    if new == old:
                        continue
                    overrides.setdefault(code, {})[name] = new
                    if name == "price_adjust_pct":  # S18: who and why, for the engine's line and for override mining
                        overrides[code]["price_adjust_by"] = decision.get("by")
                        overrides[code]["price_adjust_reason"] = decision.get("reason")
                    cur.execute("INSERT INTO feedback (email_id, field, old_value, new_value, by_user, reason) VALUES (%s,%s,%s,%s,%s,%s)",
                                (e, f"{code}.{name}", Json(old), Json(new), decision.get("by"), decision.get("reason")))  # fmt: skip
        self.w.conn.commit()
        return {"review": decision, "overrides": overrides}

    def act(self, s: JobState) -> dict[str, Any]:
        """Record the human decision (S6), then do what it allows (S8, actions/act.py): the Odoo draft quotation, the quote
        PDF and the threaded reply, each checked by OPA inside the write tool against the person's signed approval."""
        d = s["review"]
        final = (d.get("edits") or {}).get("customer_language_text") or s["draft"].get("customer_language_text")
        with self.w.conn.cursor() as cur:
            cur.execute("""INSERT INTO decisions (email_id, action, decided_by, reason, final_text) VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (email_id) DO UPDATE SET action = EXCLUDED.action, decided_by = EXCLUDED.decided_by,
                             reason = EXCLUDED.reason, final_text = EXCLUDED.final_text, decided_at = now()""",
                        (s["email_id"], d["action"], d.get("by"), d.get("reason"), final))  # fmt: skip
        self.w.conn.commit()
        return {"outcome": {"recorded": d["action"], "by": d.get("by"), **act_effects.run(self.w, dict(s), final or "")}}

    # ---- S17: suppliers
    def suppliers(self, s: JobState) -> dict[str, Any]:
        """S17 (W13, W18): drafts to 2-3 vendors for stale outside-process prices or short material, redacted, OPA-gated
        (NDA, export control). A person approves each send on the job page; the quote meanwhile uses the last known
        price, with a flag."""
        est = s.get("estimates") or []
        if not est:
            return {"supplier_rfqs": []}
        with self.w.conn.cursor() as cur:
            cur.execute("SELECT from_addr, from_name FROM emails WHERE id = %s", (s["email_id"],))
            addr, name = cur.fetchone()
        with db.connect(self.w.schema) as conn:
            out = sup.draft_all(conn, self.w.store, self.w.opa_url, s["email_id"], est, s["facts"], s.get("extraction") or {},
                                {"from_addr": addr, "from_name": name})  # fmt: skip
        return {"supplier_rfqs": out}

    def supplier_reply(self, s: JobState) -> dict[str, Any]:
        """S17: a supplier answered one of our RFQs: extract it (LLM, with provenance), then settle the best offer."""
        e = s["email_id"]
        with self.w.conn.cursor() as cur:
            cur.execute("SELECT in_reply_to FROM emails WHERE id = %s", (e,))
            irt = (cur.fetchone() or [None])[0]
            cur.execute(
                "SELECT sha256 FROM attachments WHERE email_id = %s AND verdict = 'clean' AND true_type = 'application/pdf'", (e,)
            )
            pdfs = [r[0] for r in cur.fetchall()]
        text = self._email_text(e, with_subject=True)
        for sha in pdfs:  # a PDF quotation: its text layer (the suppliers' PDFs are text, not scans)
            import pymupdf

            with pymupdf.open(stream=self.w.store.get(f"files/{sha}"), filetype="pdf") as doc:
                text += "\n" + "\n".join(p.get_text() for p in doc)
        with db.connect(self.w.schema) as conn:
            res = sup.on_reply(conn, self.w.extractor_llm, e, str(irt or ""), text, s["trace_id"])
        return {"supplier": res or {"unmatched": True}, "status": "done" if res else "triaged"}

    # ---- S15: replies and follow-ups
    @staticmethod
    def _reply_risk(route: str, **extra: Any) -> dict[str, Any]:
        return {"route": route, "escalate": [], "senior": [], "needs_senior": False, "check": [], "value_usd": 0.0, "flags": [],
                "not_costed": [], "needs_human": [], "conditions": [], **extra}  # fmt: skip

    def reply_router(self, s: JobState) -> dict[str, Any]:
        """S15 (W8, W9): what the customer's reply means (the LLM: one classification, stored), then code acts on it."""
        e = s["email_id"]
        text = self._email_text(e, with_subject=True)
        with self.w.conn.cursor() as cur:
            cur.execute("SELECT result FROM replies WHERE email_id = %s", (e,))
            row = cur.fetchone()
            cur.execute("SELECT thread_id, coalesce(sent_at, ingested_at)::date, from_name FROM emails WHERE id = %s", (e,))
            root, reply_date, name = cur.fetchone()
        if row:
            res = row[0]
        else:
            qn = reply_mod.quotation_for(self.w.conn, e, text)
            res = (
                {"quotation": qn, "route": "no_quotation"}
                if not qn
                else {"quotation": qn, **reply_mod.classify(self.w.triage_llm, text, s["trace_id"])}
            )
            with self.w.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO replies (email_id, result) VALUES (%s, %s) ON CONFLICT (email_id) DO NOTHING", (e, Json(res))
                )
            self.w.conn.commit()
        route, qn = res["route"], res.get("quotation")
        if route in ("no_quotation", "other"):
            return {"reply": res, "status": "triaged"}
        if route == "revision":
            return {"reply": res, "status": "running"}
        q = reply_mod.tool("erp-mcp-read", "get_quotation", {"name": qn})
        if route == "reject":  # W9: the lost reason, in Odoo, where Meera's reports look
            if q.get("found") and q["state"] in ("draft", "sent"):
                res["odoo"] = reply_mod.tool(
                    "erp-mcp-write", "record_lost", {"name": qn, "reason": res.get("detail") or "no reason given", "email_id": e}
                )
            else:
                res["odoo"] = {"name": qn, "skipped": f"not open in Odoo ({q.get('state') or 'not found'}): left as it is"}
            with self.w.conn.cursor() as cur:
                cur.execute("UPDATE replies SET result = %s WHERE email_id = %s", (Json(res), e))
            self.w.conn.commit()
            return {"reply": res, "status": "done"}
        with self.w.conn.cursor() as cur:  # the quote's own facts, when the agent made it (S5 estimates of the RFQ)
            cur.execute("SELECT result FROM estimates WHERE email_id = %s ORDER BY part_code", (root,))
            est = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT result->'requirements'->>'reply_language' FROM extractions WHERE email_id = %s", (root,))
            lang = (cur.fetchone() or [None])[0] or "en"
        info = {
            "quotation": qn,
            "odoo_state": q.get("state"),
            "validity_date": q.get("validity_date"),
            "detail": res.get("detail"),
        }
        raw = self._email_text(e)
        if route == "accept":
            pricing = opa.decide(self.w.opa_url, "policy/pricing_policy", {}) or {}
            with db.connect(self.w.schema) as conn:
                late = reply_mod.late_reasons(q, reply_date, conn, est, float(pricing.get("price_protection_pct", 5)))
                if late:  # W8: not at the old price; the engine re-prices today, a person approves the notice
                    lines = reply_mod.reprice(conn, q, self.w.opa_url, q.get("customer_ref"), self.w.cycle_model())
                    facts = {"mode": "reprice_notice", "reply_language": lang, "currency": "USD", "quotation": qn, "reasons": late,
                             "lines": [{k: v for k, v in ln.items() if k != "error"} for ln in lines if "new_unit_price" in ln]}  # fmt: skip
                    d = drafting.draft(self.w.drafter_llm, facts, raw, s["trace_id"])
                    return {"reply": res, "draft": {**d, "mode": "reprice_notice", "facts": facts},
                            "risk": self._reply_risk("reply_accept_late", check=late, reply={**info, "late": late, "reprice": lines})}  # fmt: skip
            text_ok = (f"Dear {name or 'customer'},\n\nThank you for accepting our quotation {qn}. We are confirming your order now "
                       "and will send the order confirmation shortly.\n\nKind regards,\nApex Precision Components, Quotations")  # fmt: skip
            return {"reply": res, "draft": {"mode": "acknowledge", "customer_language_text": text_ok, "english_text": text_ok, "faithful": True},
                    "risk": self._reply_risk("reply_accept", check=[f"confirm {qn} in Odoo (Sales → {qn} → Confirm): the agent never confirms orders"],
                                             reply=info)}  # fmt: skip
        if route == "negotiate":  # a person decides; the S14 curve (if the agent quoted it) is on the job page
            curves = {x["code"]: x["win"]["curve"] for x in est if x.get("win")}
            return {"reply": res, "draft": {"mode": "none", "customer_language_text": "", "english_text": "", "faithful": True},
                    "risk": self._reply_risk("reply_negotiate", check=[f"the customer asks: {res.get('detail') or 'a better price'}"],
                                             reply={**info, "curves": curves})}  # fmt: skip
        facts = {"mode": "answer", "reply_language": lang, "currency": "USD", "quotation": qn, "question": res.get("detail"),
                 "parts": [{"part": x["code"], "price_breaks": [{"quantity": b["qty"], "unit_price": b["unit_price"]} for b in x["breaks"]],
                            "committed_delivery_date": x["delivery"]["committed"], "valid_until": x["terms"]["validity_date"]} for x in est]}  # fmt: skip
        d = drafting.draft(self.w.drafter_llm, facts, raw, s["trace_id"])
        return {
            "reply": res,
            "draft": {**d, "mode": "answer", "facts": facts},
            "risk": self._reply_risk("reply_question", reply=info),
        }

    def follow_up(self, s: JobState) -> dict[str, Any]:
        """S15 (W9): N days after the quote went out. A reply in the thread -> nothing to do; else a plain reminder (a
        template, no LLM, no prices) for a person to approve."""
        e = s["email_id"]
        with self.w.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM emails WHERE thread_id = %s AND id <> %s", (e, e))
            replied = cur.fetchone()[0]
            cur.execute("SELECT from_name FROM emails WHERE id = %s", (e,))
            name = cur.fetchone()[0]
            cur.execute(
                "SELECT result->>'quotation', at::date FROM actions WHERE email_id = %s AND result ? 'quotation' ORDER BY id LIMIT 1",
                (e,),
            )
            qn, sent = cur.fetchone() or (None, None)
        n = int(s.get("followups") or 0)
        if replied or not qn:
            return {"followup_due": False, "followup_drafted": False, "status": "sent"}
        text = reply_mod.followup_text(qn, str(sent), name)
        return {"followup_due": False, "followup_drafted": True, "followups": n + 1,
                "draft": {"mode": "followup", "customer_language_text": text, "english_text": text, "faithful": True},
                "risk": self._reply_risk("followup", reply={"quotation": qn, "sent": str(sent), "followup": n + 1})}  # fmt: skip

    def _schedule_followup(self, s: JobState) -> None:
        """After a quote (or a reminder) went out: the next follow-up, if policy wants one."""
        mode = (s.get("draft") or {}).get("mode")
        if (s.get("outcome") or {}).get("status") != "sent" or mode not in ("quote", "followup"):
            return
        pol = opa.decide(self.w.opa_url, "policy/followups", {}) or {}
        if int(s.get("followups") or 0) >= int(pol.get("max", 0)):
            return
        with self.w.conn.cursor() as cur:
            cur.execute("UPDATE jobs SET followup_at = now() + make_interval(days => %s) WHERE email_id = %s",
                        (int(pol.get("after_days", 7)), s["email_id"]))  # fmt: skip

    def learn(self, s: JobState) -> dict[str, Any]:
        """Every human edit is data (S18 learns from it): store what the person changed, field by field."""
        edits = s["review"].get("edits") or {}
        with self.w.conn.cursor() as cur:
            for field, new in edits.items():
                old = s["draft"].get(field)
                cur.execute("INSERT INTO feedback (email_id, field, old_value, new_value, by_user, reason) VALUES (%s,%s,%s,%s,%s,%s)",
                            (s["email_id"], field, Json(old), Json(new), s["review"].get("by"), s["review"].get("reason")))  # fmt: skip
        self._schedule_followup(s)
        self.w.conn.commit()
        return {"status": (s.get("outcome") or {}).get("status") or "done"}  # sent | done

    def _email_text(self, email_id: int, with_subject: bool = False) -> str:
        with self.w.conn.cursor() as cur:
            cur.execute("SELECT raw_sha256 FROM emails WHERE id = %s", (email_id,))
            sha = cur.fetchone()[0]
            cur.execute("SELECT string_agg(filename, ' ') FROM attachments WHERE email_id = %s", (email_id,))
            files = cur.fetchone()[0] or ""
        msg = email.message_from_bytes(self.w.store.get(f"raw/{sha}.eml"), policy=email_policy.default)
        text = content.visible_text(msg)
        return f"{msg.get('Subject', '')}\n{files}\n{text}" if with_subject else text
