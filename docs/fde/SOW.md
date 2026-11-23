# Scope of work: Apex quoting agent

**Client:** Apex Precision Components (sponsor: Meera, managing director)
**Supplier:** the FDE team
**Date:** 2026-11-20 · **Status:** draft for signature

## 1. Objective
Cut the time from RFQ to quote from a median of **5.0 days** to **≤ 1 working day**, with quotes that state a price, a capacity-checked delivery date, a CO₂ figure on request and clear terms. Apex's existing systems stay in place; a human approves every outbound quote. The design is in [`SOLUTION_DESIGN.md`](SOLUTION_DESIGN.md).

## 2. In scope
| Milestone | Deliverables | Acceptance |
|---|---|---|
| **M1 Discovery & design** (S0) | `DISCOVERY.md`, `SOLUTION_DESIGN.md`, this SOW, `ARCHITECTURE.md` + ADRs, `THREAT_MODEL.md` v1 | Meera approves the scope |
| **M2 Pilot slice** (S1–S8) | Running agent stack beside the legacy system: ingestion, security gate, extraction, deterministic cost/date/CO₂/terms, review UI with roles, Odoo draft quote + PDF + threaded email | Live demo: a customer RFQ becomes an approved quote in the customer's inbox and a draft quotation in Odoo |
| **M3 Make it trustworthy** (S9–S10) | OPA policies, audit chain, kill switch, budgets, red-team suite, evals in `make check`, dashboards | Red-team green, audit chain verifies, kill switch tested, dashboards show live data |
| **M4 More value** (S11–S18, optional, each independent) | Capability check and priority, repeats and revisions, DFM, win model and capacity, follow-ups and digest, what-if assistant, supplier RFQs, quoted vs actual | Each phase's "Done when" checks pass |
| **M5 Go live & hand over** (S19–S20) | Backups with a tested restore, resilience drills, runbooks, shadow → assisted rollout, `RESULTS.md`, `HANDOVER.md` | Restore drill run; results vs baseline published; handover accepted |

## 3. Out of scope
Replacing Odoo, the mailbox or the file share; automatic pricing or sending without approval; confirming orders; changing Odoo master data without approval; any cloud hosting; a certified product carbon footprint (we provide a documented estimate).

## 4. Assumptions
- Everything runs on one local machine with Docker Compose (≈14 GB RAM for Docker).
- LLM inference uses Groq's open-weight models through one gateway; Apex accepts that prompts (drawing text, email text) are processed there, except for jobs policy keeps local.
- Historical data in Odoo is representative enough to train the cycle-time and win models; where it isn't, we say so.
- Public sources are acceptable for CO₂ factors and the commodity price index.

## 5. Client responsibilities
| Who | What | When |
|---|---|---|
| Vikram | Scoped accounts: Odoo `erp_reader` + `erp_integration`, IMAP `agent@`, a read-only share user (we provide the scripts) | Before S2 |
| Ramesh | 2 h/week reviewing extracted rules and approving them into policy; approving quotes above the limit | From S2 |
| Arjun / estimators | Reviewing drafts in the review UI, giving reasons for edits | From S7 |
| Kavita | Confirming work-center calendars and capacity assumptions | S5 |
| Meera | Decisions on approval limits, validity, price protection, data that must stay local (`DISCOVERY.md` §7) | Before S5 |

## 6. Acceptance and success measures
Each phase is accepted when its "Done when" checks pass on the real system and its `DEMO.md` section reproduces it from a fresh reset. Business success is measured against [`BASELINE_METRICS.md`](../legacy/BASELINE_METRICS.md) in `RESULTS.md`: response time, minutes per quote, on-time delivery, AI cost per quote, and win rate (reported, not guaranteed).

## 7. Change control
Anything not in §2 is a change request: written down, sized, and approved by Meera before work starts.

**Approved by (client):** ____________________ (Meera) **Date:** __________
