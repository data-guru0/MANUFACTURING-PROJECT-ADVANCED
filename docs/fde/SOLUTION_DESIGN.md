# Solution design: the Apex quoting agent (M1 / S0)

> **Status:** draft for approval, 2026-11-20. **Inputs:** [`DISCOVERY.md`](DISCOVERY.md), [`BASELINE_METRICS.md`](../legacy/BASELINE_METRICS.md), [`PAIN_POINTS.md`](../legacy/PAIN_POINTS.md). **Architecture:** [`ARCHITECTURE.md`](../ARCHITECTURE.md).

## 1. The idea in one sentence
An agent **beside** Apex's existing systems reads each RFQ, works out the part, then **calculates** (in code, never in the LLM) the price, delivery date, CO₂ and terms from Odoo's live data. It drafts the reply and puts it in front of an estimator. Nothing goes out until a human approves.

## 2. Golden rules (non-negotiable)
1. **The LLM reads, retrieves, explains and writes. Code calculates. Humans approve.** Prices, dates, CO₂ and validity terms come only from deterministic code.
2. **Every number has a source:** an Odoo record, a rate, a model version, a policy rule or an emission-factor citation.
3. **Nothing goes to a customer or supplier without approval** (a human, or an explicit OPA policy).
4. **Every AI call goes through LiteLLM**: the only holder of the Groq key, one virtual key and budget per agent.
5. **Every permission decision is made by OPA**, and it fails closed.

## 3. What we will NOT do
- **Replace Odoo, the mailbox or the share.** They stay the systems of record; we integrate with them.
- **Auto-price.** No price leaves without a human approving it.
- **Auto-send.** No email to a customer or supplier without approval (plain reminders only if an OPA policy allows them).
- **Confirm orders.** A human confirms orders in Odoo; the agent never does.
- **Write back to Odoo's master data** without approval (data fixes are reported, not applied).
- **Use the cloud.** Everything runs locally with Docker Compose; only LLM prompts go to Groq, and export-controlled jobs can be kept off it.

## 4. Fix, phase and success metric for each weakness
Baselines are from `BASELINE_METRICS.md` / `PAIN_POINTS.md` (measured 2026-11-20). Targets are **measured and reported honestly** in `docs/RESULTS.md` at S20. A missed target is reported, not hidden.

| ID | Fix | Phase | Success metric (baseline → target) |
|---|---|---|---|
| **W1** | Ingest every RFQ automatically, read the drawing, cost it and draft the reply; the estimator only reviews. The queue is sorted by value, not arrival. | S3–S8, S11 | Median reply **5.0 days → ≤ 1 working day**; estimator time per quote **≈ 2.5 h → ≤ 30 min**; queue order explained on screen |
| **W2** | One read model (app-postgres) synced from Odoo, the share and the mailbox; Odoo stays the system of record | S2 | 100 % of quote inputs carry a source; the 30 Excel-only quotes are indexed |
| **W3** | Scoped service accounts (`erp_reader`, `erp_integration`, `agent@`, read-only share user); named Keycloak users; hash-chained audit log | S2, S7, S9 | Shared logins used by the agent: **0**; actions attributed to a named person: **100 %**; audit chain verifies |
| **W4** | Normalize material names, flag duplicate customers and missing routings in the read model; data-quality report for Apex | S2 | **5/5** duplicates and **all 6** Al 6061 spellings found; BOMs without routing flagged (**53**) |
| **W5** | Prices always from Odoo; stale share prices logged as conflicts; Excel-only quotes indexed as analogs | S2, S5 | Prices from the share used in quotes: **0**; conflicts logged |
| **W6** | Ramesh's notes and override patterns become **named, versioned rules** in `policy.yaml`, approved by him; a what-if assistant answers pushback from those rules | S2, S16, S18 | ≥ 1 rule approved and shown changing a quote; what-if answers cite their tool sources |
| **W7** | Delivery date from real machine load (open work orders, calendars, vendor lead times), committed on the P90 cycle time, bottleneck named; portfolio load from open quotes | S5, S14 | On-time **69.6 % → ≥ 85 %** of committed dates met (backtest) |
| **W8** | Validity date in every quote, a material price-protection clause tied to a public commodity index; late acceptances trigger a reprice | S5, S15 | Quotes with validity and protection clause: **100 %**; late acceptances at an old price without review: **10 → 0** |
| **W9** | Follow-up drafts after N days; replies classified; lost reasons written to Odoo | S15 | Lost quotes with a reason **20.1 % → ≥ 80 %** of replied rejections |
| **W10** | Detect repeats (file hash, part no., geometry, embedding) and revisions (diff); reprice only what changed | S12 | Labelled repeats and revisions detected: **22/22**; false matches across customers: **0** |
| **W11** | Deterministic capability check against machine envelopes, tolerance, finish, materials, certificates → bid / conditions / no-bid | S11 | Out-of-capability RFQs caught **8/8**; capable parts declined: **0** |
| **W12** | DFM rules; savings recomputed by the engine and offered as optional lines (customer spec never changed) | S13 | Quotes with DFM advice **0 of 400 → 100 %** of RFQs with a seeded DFM issue; savings reproduced by the engine: **100 %** |
| **W13** | Supplier RFQs drafted to 2–3 vendors when a price is stale or stock is short; replies extracted with provenance | S17 | Supplier prices used in quotes older than policy limit: **flagged 100 %**; replies extracted from the responders |
| **W14** | Quoted vs actual per operation; variance dashboard; loss-making jobs visible; cycle-time model retrained through a gate | S18 | Loss-making jobs visible automatically (**23/98** today, found by hand in L6) |
| **W15** | CO₂ per part from material, machine energy (`x_power_kw`) and outside processes, with cited factors | S5 | CO₂ requests answered: **0 % → 100 %**, each with a method note; one part reproducible by hand |
| **W16** | Agent uses a read-only share user; backups with a tested restore; monitoring; recommendations report for Vikram | S9, S19 | Agent write access to the share: **0**; backups **0 → daily**, with **1** restore drill run and recorded; `SECURITY_RECOMMENDATIONS.md` delivered |
| **W17** | Dashboards (operations, business KPIs before/after, AI quality, cost, oversight); weekly digest | S10, S15 | KPIs visible to management **0 → 5** dashboards live (before/after against this baseline); weekly digest with **0** unfaithful numbers |
| **W18** | Supplier RFQs share only what's needed; title block and customer name redacted; OPA blocks NDA and export-controlled jobs | S9, S17 | Customer names leaving in supplier RFQs: **0**; NDA/export block tested |

**Engagement-level metrics** (also in `RESULTS.md`): AI cost per quote (target **≤ $0.25**), win rate (baseline **26.1 %**; a lagging indicator, reported, not promised), human edit rate per draft, approval time.

## 5. How a quote flows (to-be)
```
rfq@ ──IMAP──▶ ingest (MinIO, job queue) ──▶ security gate (malware, hidden text, Prompt Guard, sender)
  ▶ triage (rfq / follow-up / supplier reply / not RFQ)  ▶ extract PartSpec (DXF > STEP > PDF text > OCR+vision)
  ▶ reconcile + critic  ▶ retrieve analogs  ▶ CODE: cost · delivery date · CO₂ · terms (all with provenance)
  ▶ OPA risk policy (escalate?)  ▶ draft reply (customer's language + English)
  ▶ HUMAN REVIEW (interrupt)  ▶ act: draft quote in Odoo, PDF, threaded email from rfq@  ▶ learn from the outcome
```

## 6. Delivery plan (milestones)
| Milestone | Phases | What Meera sees |
|---|---|---|
| **M1 Discovery & design** | S0 | This document, the SOW, the architecture, the threat model |
| **M2 Pilot slice** | S1–S8 | **First demo:** a real RFQ becomes an approved, sent quote with price, date and CO₂ |
| **M3 Make it trustworthy** | S9–S10 | Governance, red-team, cost control, dashboards against the baseline |
| **M4 More value** | S11–S18 | One weakness per phase (independent extensions) |
| **M5 Go live & hand over** | S19–S20 | Backups and drills, shadow → assisted mode, `RESULTS.md`, handover |

If time runs short, **M1 + M2 + M3 + M5 is a complete project**.

## 7. Key risks to the plan
| Risk | Mitigation |
|---|---|
| Drawing extraction is less accurate than hoped (scans) | Every field shows source and confidence; low confidence → `needs_human`; DXF/STEP preferred over vision |
| Estimators don't trust the numbers | Provenance on every line, analogs shown, Ramesh's rules as named policy, shadow mode before assisted mode |
| Groq unavailable or too expensive | One gateway with fallbacks ending at "escalate to human", budgets per job and per agent, kill switch to manual mode |
| Prompt injection in customer email/PDF | Security gate, untrusted-content delimiting, no tool can send without approval, red-team suite (see `THREAT_MODEL.md`) |
