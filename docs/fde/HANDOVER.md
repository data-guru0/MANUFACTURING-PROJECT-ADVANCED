# Handover: the Apex quoting agent (S20)

_From the FDE team to Apex Precision Components: Meera (owner), Ramesh (estimating), Arjun (estimating), Kavita
(planning), Vikram (IT). What was built, how to run it, who owns what, the known limits and the next steps._

## 1. What was built
An AI quoting assistant that works **beside** Apex's existing tools (Odoo, the rfq@ mailbox, the file share) and replaces none of them:
1. Every RFQ email is read, checked for viruses and injection, sorted and understood (drawings, scans, STEP files).
2. Code, not AI, prices it: material, machine time from Odoo's rates, capacity-aware delivery dates, CO₂ on request, validity and price protection. Every line says where it came from.
3. It checks capability, repeats and revisions, design-for-manufacturing savings and stale supplier prices.
4. A person reviews a ready draft on the **review screen** (https://localhost:8448), and approves, edits, re-costs or rejects it. Only then does the agent create the Odoo quotation and send the email.
5. It follows up, reads the replies, records lost reasons in Odoo, re-prices late acceptances, and writes a weekly digest.
6. It learns: quoted vs actual, reviewers' overrides become proposed rules (Ramesh approves), and models are retrained through a gate.

The results: `docs/RESULTS.md`. The design: `docs/fde/SOLUTION_DESIGN.md`, `docs/ARCHITECTURE.md`, `docs/adr/`. Security: `docs/security/THREAT_MODEL.md`, `docs/governance/`.

## 2. How to operate it
- Day to day: **review screen** (queue, job pages, Capacity, Digest), **Grafana** (https://localhost:8444) for operations, KPIs, AI quality, cost and oversight; **Odoo** as before.
- Bad days: `docs/runbooks/RUNBOOKS.md` (stuck job, outage, injection, kill switch, restore, rollback, key rotation). The drills that proved them: `docs/runbooks/DRILLS.md`, `RESTORE_DRILL.md`.
- Every command, in order, with what you should see: `DEMO.md` (cheat sheet at the top).
- Rebuild from zero: `DEMO.md` Part II, verified by a full tear-down and rebuild on 2026-11-23 (≈30 min plus image builds; the dry run's findings are in RESULTS §0).

## 3. Who owns what
| Area | Owner | What that means |
|---|---|---|
| Approving quotes | Arjun, Ramesh (senior above the policy limit, new customers, low margin, export control) | the review screen; OPA enforces who may approve what |
| Pricing policy and rules | Ramesh, Meera | `agent/config/policy.yaml` (margins, floor/ceiling, DFM, follow-ups, suppliers); new rules only through `rules approve` |
| Prompts and models | Ramesh (approves), FDE/IT (runs) | change control in Langfuse: push → evaluate → promote with evidence |
| Capacity and dates | Kavita | the Capacity page; `expected_load_weight`, `lead_buffer_days` in policy |
| The system (accounts, backups, keys, updates) | Vikram | the runbooks; back up weekly and run the restore drill monthly |
| The AI budget and the kill switch | Meera (budget), Vikram (switch) | `agents.yaml` budgets; `make ai-off` / `ai-on` |

## 4. Known limits (and why they're acceptable for now)
- The win model is **not validated** (no better than the average); the system doesn't use it. Delivery promises are kept less often than the flat 3 weeks in the backtest. Price accuracy is 15.7 % vs a 12 % target. See RESULTS §3.
- A prototype on one machine: one worker (it rescues its own jobs by a stable container name; several workers need a lease), no high availability, backups are local folders (copy them off the machine), the Groq free tier sets the pace.
- Scanned drawings need more human checking; three DFM rules need data the drawings don't carry.
- Client-side actions we recommended but did not do (it's Apex's system): clean Odoo master data (duplicates, material names), lock down the share's write permissions, retire the shared `office` login (`docs/fde/SECURITY_RECOMMENDATIONS.md`, `fde/DATA_QUALITY.md`).

## 5. Recommended next steps
1. **Run in assisted mode for 4 weeks**, then measure win rate and turnaround again (`make baseline` vs the Grafana KPIs).
2. Fix the flanged-housing routing standards in Odoo, then retrain the cycle-time model (it will pass the gate once actual jobs finish).
3. Tune delivery promises (`lead_buffer_days`) against the backtest until promises kept are at least the measured baseline (`BASELINE_METRICS.md`), with dates still shorter than 3 weeks.
4. Move to a paid inference tier (or a local open-weight model behind LiteLLM: no code change, a `models.yaml` change).
5. Retrain the win model after ~100 more decided quotes with recorded lost reasons.
