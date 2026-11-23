"""Look at a job waiting for review, or decide it (S6). The review screen (S7) is the normal way; this CLI is the
operator's break-glass path. Both end in `QuoteGraph.resume`, which asks OPA (apex/review) whether the roles may decide.
# ponytail: --role is self-asserted (anyone who can exec into the worker container already owns the job); S9 removes it
# or makes it a signed token.

Usage: python -m apex_agent.graph.review show <email_id>
       python -m apex_agent.graph.review <approve|edit|clarify|reject> <email_id> --by <name> --role <role> [--reason <text>]
                                         [--text <the edited customer email>]
       python -m apex_agent.graph.review waiting
"""

import argparse
import json

from apex_agent.worker import Worker


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["show", "waiting", "approve", "edit", "clarify", "reject"])
    ap.add_argument("email_id", type=int, nargs="?")
    ap.add_argument("--by")
    ap.add_argument("--reason", default="")
    ap.add_argument("--role", action="append", default=[], help="Keycloak role(s) the person holds, e.g. senior_estimator")
    ap.add_argument("--text", help="edit: the customer email as the reviewer wants it")
    a = ap.parse_args()
    w = Worker()
    g = w.graph
    if a.action == "waiting":
        with w.conn.cursor() as cur:
            cur.execute("SELECT email_id FROM jobs WHERE status = 'awaiting_review' ORDER BY email_id")
            print(json.dumps({"awaiting_review": [r[0] for r in cur.fetchall()]}))
        return
    snap = g.graph.get_state(g.config(a.email_id))
    if a.action == "show":
        pending = [i.value for i in snap.interrupts]
        print(json.dumps({"status": g.status(a.email_id), "next": list(snap.next), "waiting_for": pending,
                          "spent_usd": round(snap.values.get("spent_usd", 0.0), 5), "trace_id": snap.values.get("trace_id")},
                         default=str, ensure_ascii=False, indent=1))  # fmt: skip
        return
    if not a.by:
        raise SystemExit("--by <name> is required: every decision has a named person")
    decision = {"action": a.action, "by": a.by, "reason": a.reason, "roles": a.role, "via": "cli"}
    if a.action == "edit":
        decision["edits"] = {"customer_language_text": a.text or ""}
    try:
        print(json.dumps({"email_id": a.email_id, "status": g.resume(a.email_id, decision)}))
    except PermissionError as e:
        raise SystemExit(f"not allowed: {e}") from e


if __name__ == "__main__":
    main()
