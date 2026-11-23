"""Ask OPA. Every permission decision goes through here (ADR 0004), and it FAILS CLOSED:
OPA unreachable, an error, or anything other than a literal `true` means "no".

S9: every decision is written to the audit log with OPA's decision_id (OPA keeps the full decision in its own decision
log, with tokens masked), so "who was allowed what, and why" can be traced from the audit row to the policy's answer."""

import os
import ssl
from typing import Any

import httpx

from apex_agent import audit


def _ask(opa_url: str, rule: str, input_: dict[str, Any], verify: ssl.SSLContext | bool) -> tuple[Any, str | None]:
    try:
        r = httpx.post(f"{opa_url}/v1/data/{rule}", json={"input": input_}, timeout=5, verify=verify)
        r.raise_for_status()
        body = r.json()
        result, decision_id = body.get("result"), body.get("decision_id")
    except (httpx.HTTPError, ValueError) as e:
        try:  # no answer = no (fail closed) whatever happens next; the row is best-effort here, not for a real decision
            audit.record("opa_decision", _actor(input_), {"rule": rule, "error": type(e).__name__, "result": "deny"},
                         _email(input_))  # fmt: skip
        except audit.AuditUnavailable:
            pass
        return None, None
    audit.record("opa_decision", _actor(input_), {"rule": rule, "decision_id": decision_id, "input": input_, "result": result},
                 _email(input_))  # fmt: skip
    return result, decision_id


def _actor(input_: dict[str, Any]) -> str:
    return str(input_.get("actor") or input_.get("node") or input_.get("agent") or os.environ.get("APEX_SERVICE") or "unknown")


def _email(input_: dict[str, Any]) -> int | None:
    e = input_.get("email_id")
    return e if isinstance(e, int) else None


def allowed(opa_url: str, rule: str, input_: dict[str, Any] | None = None, verify: ssl.SSLContext | bool = True) -> bool:
    """`rule` is a data path such as "apex/system/ai_enabled"."""
    return _ask(opa_url, rule, input_ or {}, verify)[0] is True


def review_denied(opa_url: str, decision: dict[str, Any], risk: dict[str, Any], email_id: int | None = None) -> list[str]:
    """S7: may these roles take this action on this job? [] means yes; otherwise OPA's reasons (fail closed)."""
    inp = {"action": decision.get("action"), "roles": decision.get("roles") or [], "value_usd": risk.get("value_usd", 0.0),
           "needs_senior": bool(risk.get("needs_senior")), "senior": risk.get("senior") or [], "email_id": email_id,
           "actor": decision.get("by")}  # fmt: skip
    d = decide(opa_url, "apex/review/decision", inp)
    if not isinstance(d, dict) or d.get("allow") is not True:
        return list(d.get("deny") or ["not allowed"]) if isinstance(d, dict) else ["policy service (OPA) unavailable"]
    return []


def decide(opa_url: str, rule: str, input_: dict[str, Any], verify: ssl.SSLContext | bool = True) -> Any:
    """A rule's full result (e.g. a decision object), or None when OPA can't answer. Callers treat None as
    the most restrictive outcome (fail closed)."""
    return _ask(opa_url, rule, input_, verify)[0]
