# Discovery: Apex Precision Components (M1 / S0)

> **Who wrote this:** the FDE team, after two weeks embedded at Apex (phases L0–L6 of this course: we "shadowed" the legacy system and measured it).
> **Status:** draft for Meera's approval, 2026-11-20.
> **Evidence:** every claim links to `docs/legacy/` and was measured on the running system. Interview notes are **role-played**, using the personas from `build-prompts/01_LEGACY_SYSTEM_BUILD.md` §1.2. In class, students replace them with their own role-play.

---

## 1. The problem in one paragraph
Apex quotes custom CNC and sheet-metal parts by hand. An RFQ waits in a shared inbox, someone reads the drawing by eye, prices it in a spreadsheet with 2023 metal prices, asks Ramesh, promises "3 weeks" and emails a PDF. **The median reply takes 5.0 days, and Apex wins 26.1 % of quotes.** Quotes answered within 1–3 days win **45 %**; after 5–10 days only **21 %**. Delivery is on time **69.6 %** of the time, and **23 of 98** finished jobs lost money (≈ $50 600). All of it is measured in [`BASELINE_METRICS.md`](../legacy/BASELINE_METRICS.md).

## 2. Stakeholder map

| Person | Role | Wants | Fears | Influence | Our approach |
|---|---|---|---|---|---|
| **Meera** | Managing director, owner, **sponsor** | More orders, better margin, faster than competitors | Paying for AI that doesn't pay back | Decides | Monthly results against the baseline; she approves scope (this document) |
| **Ramesh** | Senior estimator, 22 years | Correct quotes | "AI guessing prices"; becoming redundant | Can block adoption | The machine **never** sets a price alone; his rules become named, versioned policy; he approves large quotes |
| **Arjun** | Junior estimator | Learn fast, stop quoting wrong | Mistakes in front of customers | Daily user | A review screen that shows *why* (sources, analogs, breakdown) |
| **Kavita** | Production planner | Delivery dates the shop can meet | Sales promising impossible dates | Strong (owns capacity) | Dates come from her real machine load, with the bottleneck named |
| **Tom** | Purchasing | Current supplier prices, fewer phone calls | Chasing vendors all day | Medium | Supplier RFQs drafted for him; drawings redacted before they leave |
| **Sana** | Quality manager | Certificates and customer requirements caught | Missing an FAI/PPAP requirement | Medium | Requirements extracted and flagged on every quote |
| **Vikram** | Part-time IT admin | Security, backups, systems that stay up | Another system he can't maintain | Must sign off on security | Scoped accounts, no new internet exposure, runbooks, backups with a tested restore |

## 3. Interview notes (role-played)
- **Meera:** "We lose jobs we should win because we answer in a week. I don't even know our win rate." → *There was no win-rate report until L6 (W17).*
- **Ramesh:** "Titanium always runs long. Kestrel pays late. That's not in any system." → *12 rules exist only in his notes sheet, and 31 quotes carry a free-text override (W6).* "If a machine sets prices, I want to see every number and where it came from."
- **Arjun:** "I spend most of the time finding a similar old part and then asking Ramesh anyway." → *40 parts were quoted more than once, each time from scratch (W10).*
- **Kavita:** "Sales always writes three weeks. Half the time the 5-axis is booked solid." → *102 of 102 won orders were promised exactly 21 days; 69.6 % were on time (W7).*
- **Tom:** "I send the customer's drawing to the subcontractor, it's the quickest way." → *3 forwards of NDA customers' drawings, title block "CONFIDENTIAL" (W18).* "Half my price lists are old." → *70.8 % are older than 6 months (W13).*
- **Sana:** "Customers now ask for CO₂ per part. We have nothing." → *2 of today's 20 RFQs ask (W15), and Odoo has no CO₂ data at all.*
- **Vikram:** "Everyone uses `office` in Odoo and the same password for the share. If someone deletes the calculator, it's gone." → *W3 and W16, confirmed: recycle bin off, no backups.*

## 4. Current-state flow (as-is)
Documented step by step with screenshots in [`SOP_MANUAL_QUOTING.md`](../legacy/SOP_MANUAL_QUOTING.md).
```
Customer ─ email + drawing ─▶ rfq@ (one shared password, oldest first)            W1 W3
  ▶ read drawing by eye (scans, inches, German/Spanish/Hindi)                       W1
  ▶ search Odoo for similar parts (duplicates, 6 spellings of Al 6061)              W4 W10
  ▶ Pricing_Calculator_v7_FINAL(2).xlsx (2023 prices) + Ramesh's notes              W5 W6
  ▶ "3 weeks" (no capacity check) · subcontract? forward the whole drawing          W7 W11 W18
  ▶ type the quote into Odoo as `office` · email the PDF (no validity, no CO₂)      W3 W8 W15
  ▶ no follow-up, no lost reason, no cost-vs-actual, no metrics                     W9 W14 W17
```
The systems involved: Odoo 17, docker-mailserver + Roundcube, the Samba share, drawings as PDF/DXF/STEP, and Ramesh's head.

## 5. Weaknesses W1–W18, with evidence
The full table, with live evidence and where to see each one, is [`PAIN_POINTS.md`](../legacy/PAIN_POINTS.md). Summary:

| ID | Weakness | Key evidence |
|---|---|---|
| W1 | Manual, oldest-first, slow | median reply 5.0 d, p90 7.2 d |
| W2 | Data in ≥5 places | Odoo, share (30 Excel-only quotes), mailbox, drawings, Ramesh |
| W3 | Shared logins, no audit | 400/400 quotes by "Office (shared)" |
| W4 | Dirty master data | 5 duplicate customers, Al 6061 written 6 ways, 15 % BOMs without routing |
| W5 | Stale prices, Excel-only quotes | $5.10 vs $6.50 per kg Al 6061; 30 of 150 quotes not in Odoo |
| W6 | Rules in Ramesh's head | 12 note rules, 31 override notes |
| W7 | "3 weeks" promises | 102/102 promised 21 days; on-time 69.6 % |
| W8 | Validity not enforced | 10 accepted after expiry, all 10 lost money |
| W9 | No follow-up, few lost reasons | 20.1 % of lost quotes have a reason |
| W10 | Repeats re-quoted | 40 repeat products, 42 drawings with several revisions |
| W11 | No capability check | 4 of 4 subcontracted jobs at a loss |
| W12 | No DFM advice | 0 of 400 quote emails |
| W13 | Stale supplier prices | 70.8 % older than 6 months |
| W14 | No quoted-vs-actual | time off by 15 % on average; 23/98 jobs lost money |
| W15 | No CO₂ answer | 0 CO₂ fields in Odoo |
| W16 | Open share, no backups | one shared user, recycle bin off |
| W17 | No metrics | fields exist, on 0 screens |
| W18 | Drawings forwarded whole | 3 confidential drawings sent to suppliers |

## 6. Constraints we learned
- **Keep the systems.** Odoo, the mailbox and the share stay. Staff know them, and Vikram can't run a migration.
- **Humans approve.** Ramesh and Meera won't accept automatic prices or automatic sending (see `SOLUTION_DESIGN.md` §3).
- **Data stays local, except LLM prompts.** Only Groq (open-weight models) is allowed outside, through one gateway; export-controlled jobs can be kept off it by policy.
- **Small IT.** Everything runs with Docker Compose on one machine, with runbooks Vikram can follow.

## 7. Open questions for Apex
1. Who besides Ramesh may approve quotes above the limit, and what is the limit ($ value and margin)?
2. Which customers must never have data sent to an external LLM (beyond the export-controlled ones)?
3. Is Apex willing to state a quote validity (e.g. 30 days) and a material price-protection clause to customers?
4. Which public CO₂ factors does Apex accept (industry averages, grid factor for its country)?
5. Can Vikram give us the scoped accounts (Odoo `erp_reader`/`erp_integration`, IMAP `agent@`, read-only share user) in week 1?
