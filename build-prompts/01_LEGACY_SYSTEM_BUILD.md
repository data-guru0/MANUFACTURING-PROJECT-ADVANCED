# 01 — LEGACY SYSTEM BUILD SPEC: the client's current ("as-is") environment

> **For Claude:** Read `CLAUDE.md` and `IDEA.md` first. Build this file **phase by phase** (L0 → L6). Stop after each phase for human review. Finish all of it before you start `02_AGENTIC_SOLUTION_BUILD.md`.
>
> **The framing:** a real company, *Apex Precision Components*, runs this system today. Our team has been hired as **Forward Deployed Engineers (FDEs)** to improve it with AI. In real life the client's environment already exists. In this learning project **we build it first**, faithfully, *including its weaknesses*, so that the FDE work in file 02 has something real to integrate with and fix.
>
> **The legacy system must be realistically mediocre.** It works, the business runs on it, and it is slow, messy, inconsistent and insecure in the ways real small factories are. **Do not fix the weaknesses here.** Every weakness gets an ID (W1…W18, see §1.4), and the solution phases in file 02 fix them one by one.

---

## 0. RULES (from `CLAUDE.md`, repeated here because they matter)
1. **Nothing is mocked, faked or stubbed.** Real open-source software runs in Docker, real files are produced, real email is sent over SMTP/IMAP, and tests run against live containers. There is no `unittest.mock`, no fake servers, no hardcoded "pretend" responses, and no `TODO` code paths left at the end of a phase.
2. **Synthetic data is allowed, but fake systems are not.** We have no real factory, so the *business data* (customers, parts, history) is generated from documented engineering formulas and **loaded through the real systems' APIs**, exactly as a data migration would load it. Tag every seeded record (`x_seed_batch`) so it can be audited and wiped.
3. **Open source only.** No paid services. The legacy system uses no AI at all.
4. **This is a prototype, not a product.** Each phase must be *real and working*, but take the **simplest real implementation** that shows the capability. Don't gold-plate. Mark known shortcuts with a `# ponytail:` comment that names the limit and the upgrade path.
5. **Lab safety still applies, even though the legacy system has weaknesses.** Secrets go only in `.env` (git-ignored), passwords are generated, and ports bind to `127.0.0.1`. The *simulated* weaknesses (for example, one shared Odoo account) use strong generated passwords. We simulate bad practice; we don't create real exposure.
6. After each phase, append to `docs/PROGRESS.md` (what was built, how to run it, how it was verified, its limits), **and fill in that phase's section of `DEMO.md`**: every command in order, what it does, what you should see, what to show and what to break, all **tested for real**. Then **stop and wait for "next phase"**. In L0, also write the one-time setup section of `DEMO.md`.

---

## 1. THE CLIENT

### 1.1 Company
**Apex Precision Components** is a job shop with 40 staff. It makes CNC-machined and sheet-metal parts (brackets, gears, housings, flanges, shafts, spacers and panels) in batches of 1–5,000 for industrial customers. It receives dozens of RFQs a week, wins about 25–30 % of them, and replies in 3–5 days.

### 1.2 People (personas, also used for role-play in `LEARNING.md`)
| Persona | Role | What they care about |
|---|---|---|
| **Meera** | Managing director / owner | Winning more orders, margin, not losing customers to faster shops |
| **Ramesh** | Senior estimator, 22 years at Apex | Quotes being *right*; distrusts "AI guessing prices"; overloaded |
| **Arjun** | Junior estimator | Learning quickly; afraid of quoting wrong; keeps asking Ramesh |
| **Kavita** | Production planner | Realistic delivery dates; hates sales promising impossible dates |
| **Tom** | Purchasing | Supplier prices and lead times; spends hours chasing vendors |
| **Sana** | Quality manager | Certificates, inspection, customer requirements |
| **Vikram** | Part-time IT admin | Security, backups and keeping the old systems running |

### 1.3 Systems in place today
- **Odoo Community 17 ERP:** products, BOMs, routings, work centers, sales, manufacturing, purchasing, stock.
- **Email:** a shared `rfq@` mailbox, read through webmail.
- **File server:** `\\fileserver\Quotes` with Excel calculators, old quotes and price lists.
- **Engineering drawings:** PDF (some of them scans), DXF and STEP files, arriving as email attachments.
- **Ramesh's head:** the most important system, and the one with no documentation.

### 1.4 Deliberate weaknesses (each one is fixed by a solution phase)
| ID | Weakness | Where it lives in our build | Fixed in |
|---|---|---|---|
| W1 | Quoting is manual: about 2.5 h per quote, the inbox is worked in arrival order (FIFO), replies take 3–5 days | L5, L6 | S3–S8 |
| W2 | Quote data is scattered across 5–7 places | L1–L5 | S2 |
| W3 | One shared Odoo account ("office") for all estimators; a shared mailbox password; no record of who changed what | L1, L5 | S2, S7, S9 |
| W4 | Dirty master data: duplicate customers, material names written inconsistently ("AL6061", "6061-T6", "Alu 6061"), about 15 % of past products missing a routing | L1, L2 | S2 |
| W5 | The material price list on the share is out of date compared with Odoo; some quotes exist only in Excel | L4 | S2, S5 |
| W6 | Pricing rules live only in Ramesh's head and in a free-text "notes" sheet | L4 | S2, S18 |
| W7 | Delivery dates are promised by gut feel ("usually 3 weeks") with no capacity check, so on-time delivery is about 70 % | L2 | S5, S14 |
| W8 | Quotes have no validity date and no price protection; late acceptances at old metal prices lost money | L2 | S5, S15 |
| W9 | No follow-ups; a lost reason is recorded for only about 20 % of lost quotes | L2 | S15 |
| W10 | Repeat parts and revisions are re-quoted from scratch | L2, L5 | S12 |
| W11 | No capability check: some won jobs could not be made in-house and were outsourced at a loss | L2 | S11 |
| W12 | No design-for-manufacturing or cost-down advice to customers | — | S13 |
| W13 | Supplier prices and lead times are chased ad hoc by phone and email, so they are often stale | L2 | S17 |
| W14 | Nobody compares quoted cost with actual cost; nobody knows which jobs lose money | L2 | S18 |
| W15 | Customers ask for a CO₂ footprint per part, and Apex can't answer | L5 | S5 |
| W16 | The file share is writable by everyone; no backups; no monitoring | L4 | S9, S19 |
| W17 | No metrics: management can't see turnaround time, win rate or why quotes are lost | L6 | S10, S15 |
| W18 | Customer drawings are forwarded whole to suppliers, with no confidentiality control | L5 | S9, S17 |

Write this table, with evidence from the running system, into `docs/legacy/PAIN_POINTS.md` in L6.

### 1.5 Today's quoting flow
```
Customer ─ email RFQ + drawing ─▶ rfq@ inbox (webmail, worked in arrival order)
   │ an estimator opens it when free, often days later
   ▼
reads the drawing by eye ─▶ searches Odoo for similar parts (shared login, dirty data)
   ─▶ opens Pricing_Calculator_v7_FINAL(2).xlsx on the share (stale prices)
   ─▶ asks Ramesh ─▶ guesses a delivery date ─▶ types the quote into Odoo ─▶ emails the PDF
   (no validity date, no follow-up, no record of the outcome)
```

---

## 2. REPO LAYOUT (legacy part)
```
/
├── CLAUDE.md  IDEA.md  01_…md  02_…md  LEARNING.md  DEMO.md (classroom runbook, filled in per phase)
├── .env  .env.example  .gitignore  pyproject.toml  Makefile
├── docker-compose.legacy.yml
├── legacy/
│   ├── odoo/addons/apex_legacy/   # custom fields only
│   ├── seed/                      # costing_model.py, part_families.py, history.py, drawings.py,
│   │                              # scan_degrade.py, load_odoo.py, build_share.py
│   ├── traffic/                   # rfq_traffic.py, customer_responder.py, supplier_responder.py
│   ├── mail/  share/
├── data/reference/                # public NIST drawings + license notes
├── data/generated/                # drawings, ground truth, labels (git-ignored)
├── docs/legacy/                   # SOP_MANUAL_QUOTING.md, PAIN_POINTS.md, BASELINE_METRICS.md
├── docs/PROGRESS.md
└── tests/legacy/
```

---

## 3. PHASES

### Phase L0 — Foundations
- Set up the repo, `git init`, `uv` with Python 3.12, `ruff` and `mypy`, a `.gitignore` (covering `.env`, `data/generated/` and the volumes) and a `.env.example`. Write `scripts/gen_secrets.py`, which fills in the service passwords in `.env` and **never touches `GROQ_API_KEY`**.
- `Makefile` targets:
  - `demo-legacy`: start the legacy compose only.
  - `demo-agent`: the legacy compose plus the agent compose (used from file 02 onward).
  - `demo-reset`: wipe and reseed.
  - `seed`, `test` and `down`.
- **The legacy system must never depend on the agent stack.** The instructor must be able to show the legacy system on its own at any time.
- **Done when:** `uv sync` works and `git check-ignore .env` prints `.env`.

### Phase L1 — ERP: Odoo Community 17
- Compose: `postgres:16` and `odoo:17.0`. Install `sale_management`, `mrp`, `stock`, `purchase` and `contacts` with a script through the API.
- `apex_legacy` addon (fields only):
  - on products: `x_drawing_no`, `x_material_grade`, `x_part_family` and `x_seed_batch`;
  - on work centers: `x_max_x/y/z_mm`, `x_max_weight_kg`, `x_best_tolerance_mm`, `x_best_ra` and `x_power_kw` (needed later for CO₂);
  - on sale orders: `x_rfq_received_at`, `x_quote_sent_at` and `x_lost_reason`;
  - on partners: `x_nda_restricted`, `x_required_certs` and `x_language`.
- **Work centers** with real-world rates and setup times: CNC 3-axis mill $85/h, 5-axis $140/h, lathe $75/h, fiber laser $110/h, press brake $70/h, deburr $40/h, CMM $90/h, saw $45/h. Each has a working calendar (shifts).
- **Materials** with density and $/kg (Al 6061-T6, Al 7075-T6, SS304, SS316, steel 1018/S235, brass C360, POM, Ti-6Al-4V). **Outside processes** as service products (anodize, plating, powder coat, passivation, heat treat).
- **Legacy weakness W3:** create *one* shared internal user, `office`, that all the estimators use. Do not create scoped API users; that is FDE work in S2.
- **Done when:** a test logs in over JSON-RPC and lists the work centers, materials and processes.

### Phase L2 — Costing knowledge and synthetic history
- `costing_model.py` is **the legacy cost logic**, documented in its docstrings:
  - stock size = bounding box + allowance, rounded to a standard size;
  - material cost = volume × density × $/kg × scrap factor;
  - cycle time from removed volume, holes, threads and pockets, multiplied by tolerance and finish factors, plus the number of setups;
  - setup cost amortized over the quantity;
  - inspection time;
  - outside processes, with lot minimums;
  - overhead %, then margin % by customer tier and quantity break.
- `part_families.py`: parametric families (L-bracket, U-bracket, flanged housing, spur gear, shaft with keyway, spacer, mounting plate, sheet-metal panel).
- `history.py` generates **about 400 historical quotes over 3 years** for 30–40 fictional customers. Each quote has a product, a BOM and a routing; the price is the model's result plus **human noise** (σ ≈ 10 %) and occasional overrides by Ramesh (with a note); the win rate is about 25–30 %. Load everything through the API.
- **Seed the weaknesses into the data:**
  - **W4:** about 5 duplicate customers, inconsistent material names, and about 15 % of products with no routing.
  - **W7:** delivery dates promised as a fixed "3 weeks" regardless of load, with actual completion dates giving about 70 % on time.
  - **W8:** about 10 quotes accepted months later at old prices, where the actual material cost made the job lose money.
  - **W9:** a lost reason on only about 20 % of lost quotes.
  - **W11:** a few jobs outsourced at a loss.
- **Operational data:**
  - open MOs and work orders loading the machines at 60–90 % for the next 6 weeks;
  - completed MOs for won jobs, with **actual** times and scrap that differ from the estimate (**W14** data);
  - 10–15 suppliers with `product.supplierinfo` prices and lead times, some of them over 6 months old (**W13**);
  - stock on hand;
  - about 40 parts with Rev A and Rev B drawings;
  - customer flags for NDA status, required certificates and language.
- **Done when:** `make seed` is idempotent (a second run creates no duplicates), and a test checks the counts, that won order totals equal their line sums, and that each seeded weakness is present.

### Phase L3 — Engineering drawings
- `drawings.py` uses **ezdxf** to make a 2D drawing for each part. Each drawing has three views, real dimension entities, hole and thread callouts, tolerances, GD&T where the part needs it, finish symbols, general notes (ISO 2768-m) and a title block. Export DXF and PDF, plus **STEP via CadQuery** for about 30 % of parts.
- `scan_degrade.py` turns about 25 % of the RFQ drawings into realistic bad scans: a small rotation, noise and JPEG artifacts, in an image-only PDF. Add variety: inch drawings, the material given only in the email body, and multi-sheet drawings.
- Download the **NIST MBE PMI** public-domain test drawings into `data/reference/nist/` along with their license notes.
- Save **ground truth** JSON for every generated drawing in `data/generated/ground_truth/`. This is used for evaluation only and is never given to the agent at runtime.
- **Done when:** drawings are attached to their Odoo products, and a test parses a random sample of the DXFs and PDFs.

### Phase L4 — The file share (messy on purpose)
- A Samba container serves `\\fileserver\Quotes`, **writable by everyone** (**W16**, simulated with one shared generated credential).
- `build_share.py` writes, using openpyxl **with real formulas**:
  - `Pricing_Calculator_v7_FINAL(2).xlsx`: the same logic as `costing_model.py`, plus a **"Ramesh notes"** sheet of free-text rules of thumb (**W6**);
  - `Material_Prices_2023.xlsx` (stale, **W5**);
  - `Machine_Rates.xlsx`;
  - about 150 `Customers/<name>/Quote_<date>.xlsx` files, some of which are **not in Odoo** (**W5**).
- Recalculate headless with LibreOffice and assert that the results match `costing_model.py` within 0.5 %.
- **Done when:** the share is reachable over SMB and the recalculation test passes.

### Phase L5 — Email, RFQ traffic and the outside world
- `docker-mailserver` (real SMTP/IMAP, DKIM, TLS with a self-signed CA) with three domains:
  - `apex.local`: `rfq@` (one shared password, **W3**), `estimator1@`, `senior@`, `purchasing@`;
  - `customers.local`: one mailbox per customer;
  - `suppliers.local`: one mailbox per vendor.
- **Roundcube** webmail for the human users.
- `rfq_traffic.py` sends **labelled** RFQs over authenticated SMTP. The labels go to `data/generated/rfq_labels.jsonl`. The mix includes:
  - single-part and multi-part RFQs; PDF, scan, DXF, STEP and ZIP attachments, or no attachment;
  - repeat parts and revisions (**W10**);
  - German, Spanish and Hindi RFQs;
  - requests for certificates, FAI or PPAP, export-control statements, Incoterms and a requested delivery date;
  - **requests for a CO₂ footprint** (**W15**);
  - out-of-capability parts;
  - non-RFQs (an invoice, a newsletter, a CV);
  - **adversarial emails**: hidden instructions in the body or PDF, and misleading file extensions.
- Modes: `--burst N` and `--drip` (Poisson arrivals).
- `customer_responder.py` and `supplier_responder.py` simulate the outside parties **over real email**. Customers reply with accept, reject (with a reason), negotiate, a question or silence. Suppliers reply with a price and lead time taken from their Odoo pricelist plus noise, as free text or a PDF. In class, a human can reply by hand in Roundcube instead.
- **Done when:** `--burst 20` delivers 20 emails, which a test reads back over IMAP with their attachments and DKIM headers.

### Phase L6 — The manual process and the baseline (the as-is picture)
- `docs/legacy/SOP_MANUAL_QUOTING.md`: how an estimator quotes today, step by step, with screenshots from the running system.
- `docs/legacy/PAIN_POINTS.md`: the W1–W18 table, each with **evidence from the running system** (queries, screenshots or file names).
- `scripts/baseline_metrics.py`: from the IMAP timestamps and Odoo data, compute the median and p90 response time, win rate, win rate by response-time bucket, on-time delivery %, the share of lost quotes with a known reason, and the number of loss-making jobs. Write the results to `docs/legacy/BASELINE_METRICS.md`.
- Students quote 3 RFQs by hand following the SOP and log their minutes per step in that same file.
- **Done when:** the baseline report renders, and every weakness has evidence.

---

## 4. LEGACY DEFINITION OF DONE
- [ ] `make demo-reset && make demo-legacy` brings up a healthy, seeded Odoo, mail server, webmail and file share.
- [ ] All weaknesses W1–W18 are present and documented with evidence.
- [ ] RFQ traffic and the external responders work over real email.
- [ ] The baseline metrics are recorded.
- [ ] Zero mocks, tests run against live containers, no secrets in git.
