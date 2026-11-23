# Results: Apex quoting, before and after the agent (S20)

_For Meera (managing director), Ramesh (senior estimator) and Vikram (IT). Every number below was measured on the running
system during the build (2026-11-20 → 2026-11-23) and can be regenerated with the command next to it. "Before" is L6's
baseline (`docs/legacy/BASELINE_METRICS.md`), measured from the legacy system's own mailbox and Odoo._

## 0. Reproduced from zero (dry run, 2026-11-23)

Everything was torn down except the Docker images and rebuilt by following `DEMO.md` Part II step by step, then every
phase was taught once on the 20-email class set (`make rfq-class`). The numbers below come from that run; the rest of
this report is the build-time engagement (a bigger sample: 165 drafted jobs).

| Measure | Dry run | Note |
|---|---|---|
| Baseline (L6), measured again | median **4.9 days**, win rate **27.2 %**, on time **71.4 %**, lost reason known **20.1 %**, **20 of 101** finished jobs lost **$62,719** | the synthetic history is generated relative to the reset day, so these move by a few points per reset |
| Triage + security gate | **20/20** correct, injection recall **1.0**, **0** red banners on clean email, all 5 attacks caught | `make triage-eval` |
| RFQ in → draft ready for review | median **3.2 min**, p90 **6.0 min** (19 class RFQs) | `graph_events` |
| AI cost | **$0.0017** per drafted job | LiteLLM cost header |
| Extraction | material **0.96–0.97**, size **0.96**, quantities and every email requirement **1.0** | `documents.evaluate`, `make evals` |
| Repeats / revisions / capability / DFM | 10/10, 12/12, 0 cross-customer; 4/4 impossible parts caught, 0 capable refused; 35/35 DFM issues, 34/34 savings reproduced | the S11–S13 evals |
| Replies | 3/3 routed right; 2 lost reasons written into Odoo; the late acceptance of S00388 re-priced **−29.3 %** | `scripts.replies_eval` |
| Suppliers | 3 RFQs sent with redacted drawings, 0 leaks, 2/2 offer lines + 1 decline extracted, best price → Odoo + draft PO | `scripts.suppliers_eval` |
| Mined rule | 3 overrides by Ramesh → R-003 (+10 % for 7075) → the next 7075 quote **$169.27 → $182.29** | S18 |
| Drills + restore | groq-down, odoo-down, worker-kill, burst 100: **all passed**; restore **passed** (3,925 audit rows, 109 files re-hashed, 0 mismatches) | `scripts.drills`, `scripts.restore_drill` |

**Bugs the dry run found (all fixed, re-verified):**
- a noisy scan's revision was treated as a **repeat** (local OCR misread the rev letter), and a re-quote sent by part number alone was not recognised: the repeat check now runs after extraction and uses its reading;
- approving a quote for a material with **no price index** (POM) crashed the action step;
- a worker container that was **recreated** (not just restarted) left its job stuck: the worker now has a stable name;
- the faithfulness check flagged the "3" of **MILL3** as an invented number;
- the Business-KPI dashboard had the **baseline typed in**; it is now read from `BASELINE_METRICS.md`;
- the burst drill handed 99 of 100 jobs to people (one was in flight) and the last one reached the AI;
- `make rfq-class` sent 1 email instead of 19 (commas inside a Make function); the class set now also has the hidden-PDF-text attack;
- tests with build-time job ids / counts hard-coded; the LLM call for revision summaries sat inside the "no LLM" costing package.

## 1. Headline numbers

| Measure | Before (legacy) | After (the agent) | How measured |
|---|---|---|---|
| Response time, RFQ in → quote ready | median **5.0 days** (p90 7.2; 4.9 on the dry run's reset) | median **5.8 minutes** to a draft ready for review (p90 4.2 h: Groq quota waits); a person still approves | IMAP vs Odoo / `graph_events` (165 drafted jobs) |
| Same parts, people vs agent (shadow) | people took a median **5.6 days** (5.5 on the dry run) | agent draft in a median **7.2 minutes** (3.0 on the dry run; 5 repeats compared, median price gap 18.6 %) | `python -m apex_agent.governance.shadow` → `fde/SHADOW_REPORT.md` |
| Quotes answered with price, date, CO₂ on request, terms | by hand, no CO₂, no validity | every costed RFQ: price breaks, committed + rush date, CO₂ on request, validity + price protection, each line sourced | S5, S6 |
| Lost quotes with a known reason | **20.1 %** | **100 %** of the replies routed (4 of 4 declines; the reason written into Odoo) | `scripts.replies_eval` |
| Loss-making jobs visible | found by hand in L6 (24 of 97) | on a dashboard, live: **24 of 97, $51.5k**, plus the cause (flanged housings run **27 %** over plan) | Grafana *Quoted vs actual* |
| Win rate | 26.2 % | **not measurable yet**: 9 agent quotes sent, 0 won, 3 lost (too few, too recent) | Odoo outcomes |
| On-time delivery (promises kept) | **63.4 %** (flat "3 weeks") | backtest: committed dates met **37.5 %**, but quoted **shorter** (median 15 vs 21 days); **worse, see §3** | `costing.evaluate` |
| AI cost per quote | n/a | **$0.0011** per drafted job; **$0.0006** per quote sent (Groq free tier prices via LiteLLM) | `graph_events`, `make governance-report` |
| Price accuracy vs history | n/a | price MdAPE **15.7 %** on 79 held-out quotes (target 12 %: missed); 16.9 % after the mined 7075 rule | `costing.evaluate` |

## 2. The 18 weaknesses

Status: **Fixed** = the Done-when target was met and measured; **Partly** = built, but a target was missed or a part is left; **Not** = not addressed.

| W | Weakness | Status | Evidence |
|---|---|---|---|
| W1 | Manual quoting, FIFO inbox, 3–5 day replies | **Fixed** | drafts in minutes; the queue is sorted by P(win) × value × fit × urgency with the factors shown (S11) |
| W2 | Quote data scattered over 5–7 places | **Fixed** | one read model synced from Odoo + the share (S2); retrieval top-5 97.5 % |
| W3 | Shared logins, no record of who did what | **Fixed** | scoped Odoo/mail/MinIO/Postgres accounts per service; Keycloak users; every decision and AI call in a hash-chained audit log (8,079 rows verified after a restore) |
| W4 | Dirty master data | **Partly** | 5 duplicate customers and 35 material spellings found and mapped in the agent (`fde/DATA_QUALITY.md`); Odoo itself is not cleaned (a client decision, listed in the handover) |
| W5 | Stale price list on the share, Excel-only quotes | **Fixed** | Odoo wins, conflicts logged (9 stale prices); 30 Excel-only quotes in the read model |
| W6 | Pricing rules only in Ramesh's head | **Partly** | his notes became 2 approved rules (S2); his repeated overrides became rule R-003 (S18); the what-if assistant answers pushback (S16). **But** the win model doesn't beat the average win rate (§3) |
| W7 | Dates promised by gut feel, ~70 % on time | **Partly** | capacity-aware dates and an expected-load heatmap with over-commitment flags (S5, S14); **but** the backtest keeps fewer promises than the flat 3 weeks (§3) |
| W8 | No validity, late acceptances at old prices | **Fixed** | validity + an index clause on every quote; a late acceptance is re-priced before anyone confirms (S15) |
| W9 | No follow-ups, 20 % lost reasons | **Fixed** | reminders after 7 days; replies routed 5 of 5; lost reasons into Odoo (S15) |
| W10 | Repeats and revisions quoted from scratch | **Fixed** | 10/10 repeats, 12/12 revisions, 0 cross-customer matches; the change of a revision priced line by line (S12) |
| W11 | No capability check | **Fixed** | 8/8 impossible parts caught, 0 capable parts refused; polite declines for approval (S11) |
| W12 | No DFM advice | **Fixed** | 68/68 seeded issues, every saving reproduced by the engine to the cent, offered as optional lines only (S13) |
| W13 | Stale supplier prices | **Fixed** | stale prices trigger supplier RFQs; replies extracted 2/2; the best becomes a fresh Odoo price and a draft PO (S17) |
| W14 | Nobody compares quoted vs actual | **Fixed** | the variance dashboard and the cycle-time model retrained through its gate (S18) |
| W15 | No CO₂ per part | **Fixed** | CO₂ per part from material, machine energy and outside processes, with cited factors; reproducible by hand (S5) |
| W16 | Writable share, no backups, no monitoring | **Partly** | **the share itself is still writable by everyone** (Apex's change: `fde/SECURITY_RECOMMENDATIONS.md`); the agent reads it read-only; backups with a **passed restore drill**; 4 failure drills passed; Grafana + alerts (S9, S10, S19). The share's own permissions are a client action (handover) |
| W17 | No metrics | **Fixed** | 6 Grafana dashboards, a faithful weekly digest (S10, S15) |
| W18 | Drawings forwarded whole to suppliers | **Fixed** | redacted drawings, 0 leaks found in the supplier mailboxes, NDA/export blocks (S17) |

**Count: 14 fixed, 4 partly, 0 not addressed.**

## 3. Honest failure cases (what didn't work, and what we'd do)
1. **The win model is no better than guessing the average** (Brier 0.2006 vs 0.2000 on a time split, S14). The system refuses to use it: the queue priority keeps the analog win rate, and the margin curve shows a red "NOT validated" badge. *Next:* collect the human edits, lost reasons and competitor feedback that S15/S18 now record, then retrain.
2. **Delivery promises: shorter, but kept less often** (37.5 % vs 63.7 % in the backtest). The planner is optimistic against the history's real delivery times. *Next:* raise `lead_buffer_days` or the P90 quantile in policy, and re-run the backtest; the expected-load booking (S14) already moves dates out.
3. **Price accuracy target missed** (15.7 % vs a 12 % target). The flanged-housing family is the worst (its routing standards run 27 % short, S18). *Next:* fix those standards in Odoo (Ramesh), retrain the cycle-time model through the gate.
4. **Scanned drawings:** size is read correctly far less often than on DXF/PDF (S4). Those RFQs reach a person with "check" flags.
5. **Some DFM rules aren't checkable:** deep holes, small internal radii and thin walls, because the drawings' extracted data has no depth, radius or wall thickness (S13).
6. **The Groq free tier is the operational bottleneck:** daily quotas ran out several times. Jobs wait and resume by design (S19), but a live business needs a paid tier.
7. **Bugs found by the evaluations during the build** (all fixed, listed so nobody repeats them): a capability check that refused 9 makeable parts; a fail-open export-control rule (a boolean `true` slipped through); an assistant that quoted the old price from a confusingly named field; outages that failed jobs instead of waiting. Details in `docs/PROGRESS.md`.

## 4. Shadow and assisted mode
- **Shadow mode** (the agent drafts, people quote as before): measured on the parts people had already quoted. See `fde/SHADOW_REPORT.md`. A price gap is not an error by itself; the human prices include gut-feel adjustments and old material prices.
- **Assisted mode** (the agent drafts, a person approves, the agent sends): running since S8. 9 quotes were approved and sent through the signed-approval path; nothing has ever been sent without a person's signature (OPA checks it inside the mail tool).

## 5. Regenerate these numbers
`make baseline` (before) · `… run --rm knowledge python -m apex_agent.costing.evaluate` · `… run --rm knowledge python -m apex_agent.governance.shadow` · `make governance-report` · `uv run python -m scripts.replies_eval` · Grafana dashboards. The full command list is in `DEMO.md` (cheat sheet).
