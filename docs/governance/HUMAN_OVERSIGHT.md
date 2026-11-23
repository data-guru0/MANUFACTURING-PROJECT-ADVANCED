# Human oversight statement (S9)

**What the AI may do on its own:** read incoming email and attachments, check them, classify them, read drawings, compute
prices, dates and CO₂ **with code**, and draft a reply. **What it may never do on its own:** send anything to a customer
or supplier, create anything in Odoo, or confirm an order. Every outbound action needs a named person's approval, made in
the review screen and signed for exactly that content (S8).

**Who decides what** (Keycloak roles, enforced by OPA, `agent/policy/rego/review.rego` + `escalation.rego`):
| Decision | Estimator (Arjun) | Senior estimator (Ramesh) | Planner (Kavita), auditor (Meera), admin (Vikram) |
|---|---|---|---|
| Approve / edit a quote up to $5,000, known customer, margin ≥ 12 %, no export control | yes | yes | no |
| Approve / edit above that, for a new customer, a low margin or export control | no | yes | no |
| Clarify, reject, re-cost | yes | yes | no |
| Switch the AI off (kill switch) | — | — | Vikram, with a reason (logged) |
| Promote a prompt or a model | — | yes, with a fresh evaluation | — |
These limits are data in `agent/config/policy.yaml` (Meera's decisions), not code.

**How we know the oversight is real, not a rubber stamp:**
- **Canaries:** about 5 % of the jobs that reach review get a synthetic copy whose email states a price 15 % above the computed one. A careful reviewer edits or rejects it; approving it counts as *missed*. Canaries are never sent, and the review screen can't tell them apart.
- **Per-reviewer measures** in `docs/fde/GOVERNANCE_REPORT.md` (`make governance-report`): decisions, the share edited or re-costed, waiting time, canaries caught and missed.
- **What we do with them:** a reviewer who misses canaries reviews alongside Ramesh for a week; a rising approval rate with no edits triggers a review of the drafts' quality (S10 evals).

**Everything is traceable:** each decision, edit (as a diff), LLM call, tool call, OPA decision and outbound action is in the hash-chained audit log (`make audit-verify`).

**Manual mode is always available:** `make ai-off BY=<name> WHY=<reason>` stops every AI step and blocks every agent key; Apex quotes the legacy way (Roundcube, Odoo, the calculator) until `make ai-on`.
