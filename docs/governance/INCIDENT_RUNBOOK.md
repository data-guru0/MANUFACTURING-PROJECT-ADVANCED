# AI incident runbook (S9)

An **AI incident** is anything where the AI part of the system did, or could have done, harm: a wrong price or date sent,
customer data in the wrong place, an injection that changed behaviour, runaway cost, a reviewer approving a canary
repeatedly, or a provider outage. **Owner:** Vikram (operates), Meera (decides), the FDE team (fixes).

## 1. Contain (minutes)
| If | Do | Command / where |
|---|---|---|
| Anything might go out wrong, or you don't know yet | **Kill switch**: manual mode now | `make ai-off BY=Vikram WHY="<what you saw>"` → every AI step denied, every agent key blocked; check `make ai-status` |
| One agent misbehaves or burns money | Block only its key | LiteLLM UI (https://localhost:8446/ui) → Keys → `agent-<name>` → Block |
| A suspicious email | Leave it: it is already flagged (red banner) and escalated; never approve it | Review screen → the job → risk |
| Groq quota/outage | Nothing to contain: jobs wait (`QuotaExhausted`) and retry | Grafana / jobs table |

## 2. Understand (hours)
1. **What happened, in order:** the audit log for the job: `SELECT at, kind, actor, data FROM audit_log WHERE email_id = <id> ORDER BY id` (role `apex` or Grafana's `grafana_ro`). Verify it wasn't tampered with: `make audit-verify`.
2. **Why a permission was given:** each `opa_decision` row has OPA's `decision_id`; find it in Docker Desktop → `apex-opa-1` → Logs (tokens and headers are masked).
3. **What the model saw and said:** the `trace_id` on the job → Langfuse (https://localhost:8443) → the trace.
4. **What went out:** the `action` rows (quotation name, Message-ID); rfq@ → Sent in Roundcube; the Odoo quotation.

## 3. Correct
- A wrong quote already sent: Ramesh writes the correction by hand from rfq@; cancel the draft quotation in Odoo with the reason.
- A prompt or model at fault: fix it → `uv run python -m agent.ops.prompts push` → run the evaluation → `promote … --by Ramesh --evidence <report>`. `make check` fails until code and production match.
- A policy gap: change `agent/policy/rego` or `agent/config/policy.yaml` with a test, `make policy`.

## 4. Recover and learn
- `make ai-on BY=Vikram WHY="<fixed by …>"` when the cause is fixed and tested.
- Add a row to [AI_RISK_REGISTER.md](AI_RISK_REGISTER.md) or update its likelihood; add the attack to `tests/security/test_redteam.py` if it was one.
- Within a week: a short write-up (what, impact, cause, fix, prevention) in `docs/governance/incidents/<date>.md`.

**Drill:** the kill switch is exercised by `make check` (`test_the_kill_switch_drops_everything_into_manual_mode_and_back`) every time.
