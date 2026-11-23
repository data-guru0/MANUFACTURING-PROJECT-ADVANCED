"""The quote graph's state (S6). Plain JSON-able dicts, so the Postgres checkpointer can store every step.

Designed to grow: each phase adds its own keys (S11 adds `bid`, S12 `repeat`, S14 `win`...) without touching the
others. Values written by parallel branches (the per-part fan-out) use a reducer that ADDS instead of overwriting.
"""

import operator
from typing import Annotated, Any, TypedDict


def by_part(old: list[dict[str, Any]] | None, new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The fan-out's join: one estimate per part code, the newest wins (a re-cost after a reviewer's edit, S7)."""
    merged = {e["code"]: e for e in old or []}
    merged.update({e["code"]: e for e in new})
    return list(merged.values())


class JobState(TypedDict, total=False):
    email_id: int
    trace_id: str  # one Langfuse trace for the whole job, across restarts (it lives in the checkpoint)
    spent_usd: Annotated[float, operator.add]  # LLM spend of this job (LiteLLM cost header), summed over nodes
    gate: dict[str, Any]  # red banner, reasons, triage category, known customer
    extraction: dict[str, Any]  # S4 Extraction (parts, requirements, needs_human, clarification, route)
    ready: list[dict[str, Any]]  # parts with enough to cost: {code, part, quantities}
    not_costed: list[str]  # parts that can't be costed yet, and why
    facts: dict[str, Any]  # live Odoo facts for costing (rates, prices, customer, workload), fetched once
    estimates: Annotated[list[dict[str, Any]], by_part]  # one per part, from the fan-out
    overrides: dict[str, dict[str, Any]]  # S7: reviewer's field values per part code (re-cost), applied in reconcile
    repeat: dict[str, Any]  # S12: per part key {kind: repeat | revision | new, method, of, why, diff, summary}
    supplier_rfqs: list[dict[str, Any]]  # S17: per need {code, why, vendors, status, blocked}
    supplier: dict[str, Any]  # S17: a supplier reply's extraction and settlement
    reply: dict[str, Any]  # S15: a customer's reply to a quote {quotation, route, detail, confidence, odoo}
    followup_due: bool  # S15: set by the worker when the job's follow-up date has come
    followup_drafted: bool
    followups: int  # S15: reminders drafted so far
    capability: dict[str, Any]  # S11: per part code {decision: bid | bid_with_conditions | no_bid, reasons, conditions}
    risk: dict[str, Any]  # OPA apex/risk/decision: route, escalate reasons, senior reasons
    draft: dict[str, Any]  # customer-language text, English copy, faithfulness result
    review: dict[str, Any]  # the human's decision (from interrupt / Command(resume=...))
    outcome: dict[str, Any]  # what act did
    status: str  # triaged | awaiting_review | done | sent | manual (the jobs table mirrors it)
    manual_reason: str  # S9: why a person handles this job without the AI (e.g. export control kept local)
