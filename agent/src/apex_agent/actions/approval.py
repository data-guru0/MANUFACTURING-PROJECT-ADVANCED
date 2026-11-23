"""The approval token (S8): what a person approved, in a form the side-effect tools can check.

The review screen signs it (HS256, APEX_APPROVAL_KEY) when a person approves, edits or asks a clarifying question.
Only the screen and OPA hold that key, never the worker. Each write tool (erp-mcp-write, mail-mcp) passes the token
with hashes of what it is about to do to OPA (apex/actions/allow). So the worker can only send what a person approved,
to the person it was approved for, at the prices that were approved.

    text_sha256    recipient + subject + body of the email
    prices_sha256  every part's price breaks (code, quantity, unit price rounded to cents)
"""

import hashlib
import json
import time
from typing import Any

import jwt

TTL_S = 24 * 3600  # ponytail: a day to act on an approval; a stale one needs a new decision


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def reply_subject(draft_subject: str | None, rfq_subject: str | None) -> str:
    """The screen (which signs) and the worker (which sends) must agree on the subject to the character."""
    return draft_subject or f"Re: {rfq_subject or ''}".strip()


def text_sha256(to: str, subject: str, body: str) -> str:
    return _sha([to.strip().lower(), subject.strip(), body.strip()])


def price_lines(estimates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Estimates (S5) -> [{code, breaks: [{qty, unit_price}], options}]: what goes into Odoo, and what the hash covers.
    `options` (S13): the offered DFM alternatives, Odoo optional lines; their prices are signed like every other price."""
    from apex_agent.costing.dfm import option_label

    return [{"code": e["code"], "breaks": [{"qty": int(b["qty"]), "unit_price": round(float(b["unit_price"]), 2)} for b in e["breaks"]],
             "options": [{"label": option_label(e["code"], d), "qty": int(d["qty"]), "unit_price": round(float(d["alt_unit_price"]), 2)}
                         for d in (e.get("dfm") or {}).get("suggestions", []) if d.get("offered")]}
            for e in sorted(estimates, key=lambda e: e["code"])]  # fmt: skip


def prices_sha256(lines: list[dict[str, Any]]) -> str:
    return _sha(sorted([ln["code"], [[int(b["qty"]), round(float(b["unit_price"]), 2)] for b in ln["breaks"]],
                        sorted([o["label"], int(o["qty"]), round(float(o["unit_price"]), 2)] for o in ln.get("options") or [])]
                       for ln in lines))  # fmt: skip


def sign(key: str, *, email_id: int, action: str, by: str, roles: list[str], needs_senior: bool, senior: list[str],
         text_sha: str, prices_sha: str, **extra: Any) -> str:  # fmt: skip
    now = int(time.time())
    claims = {"iss": "apex-review-ui", "sub": by, "email_id": email_id, "action": action, "roles": roles,
              "needs_senior": needs_senior, "senior": senior, "text_sha256": text_sha, "prices_sha256": prices_sha,
              "iat": now, "exp": now + TTL_S, **extra}  # fmt: skip
    return jwt.encode(claims, key, algorithm="HS256")


if __name__ == "__main__":  # self-check: python -m apex_agent.actions.approval
    lines = price_lines(
        [{"code": "B", "breaks": [{"qty": 10, "unit_price": 12.344}]}, {"code": "A", "breaks": [{"qty": 5.0, "unit_price": 3}]}]
    )
    assert [ln["code"] for ln in lines] == ["A", "B"] and lines[1]["breaks"] == [{"qty": 10, "unit_price": 12.34}]
    assert prices_sha256(lines) == prices_sha256(list(reversed(lines)))  # order doesn't matter
    assert prices_sha256(lines) != prices_sha256(
        [{**lines[0], "breaks": [{"qty": 5, "unit_price": 3.01}]}, lines[1]]
    )  # a cent does
    opt = [{**lines[0], "options": [{"label": "alt", "qty": 5, "unit_price": 2.5}]}, lines[1]]
    assert prices_sha256(opt) != prices_sha256(lines)  # S13: an optional line's price is signed too
    assert prices_sha256(opt) != prices_sha256([{**opt[0], "options": [{"label": "alt", "qty": 5, "unit_price": 2.4}]}, lines[1]])
    assert text_sha256(" A@x.local", "S", "Body ") == text_sha256("a@x.local", "S", "Body")
    assert text_sha256("a@x.local", "S", "Body") != text_sha256("b@x.local", "S", "Body")  # another recipient
    print("approval self-check ok")
