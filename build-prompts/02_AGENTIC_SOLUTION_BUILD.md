# 02 — AGENTIC SOLUTION BUILD SPEC: the FDE engagement ("to-be")

> **For Claude:** Start only after `01_LEGACY_SYSTEM_BUILD.md` is done. Read `CLAUDE.md` first. Build **phase by phase** (S0 → S20) and stop after each phase for human review.
>
> **The framing:** Apex has hired us as **Forward Deployed Engineers**. FDEs embed with the client, learn how the business actually works, and ship working software **into the client's existing environment**. We do **not** replace Odoo, the mailbox or the file share. We integrate with them, fix the weaknesses W1–W18 from file 01, and hand over a system the client can run.
> Every phase lists the weaknesses it **Fixes**. If a phase fixes nothing for the client, it doesn't belong.

---

## 0. RULES

### 0.1 Nothing is mocked, faked or stubbed
- There are no mock LLMs, no fake tool results, no canned JSON standing in for model output, no `MagicMock` and no "demo mode" that skips a real system.
- Tests run against the real Odoo, IMAP/SMTP, Samba, Postgres, MinIO, Keycloak, OPA, LiteLLM and the **real Groq API**. Tests that call the LLM are marked `@pytest.mark.llm` so they can be run on their own, but they are never faked.
- If a dependency is down, **degrade honestly**: queue the work, alert, or hand it to a human. Never invent output.

### 0.2 Prototype, not product
This is a **real, working prototype** for learning. Every feature works for real from end to end, but:
- Build the **simplest real implementation** of each capability (simple algorithms, minimal config, one happy path plus the important failure paths). Mark shortcuts with a `# ponytail:` comment that names the limit and the upgrade path.
- Eval thresholds are **targets to measure and report honestly**, not blockers that justify endless tuning. If a target is missed, record it in `docs/PROGRESS.md` with the reason, and move on.
- Security and governance controls are **real but minimal**: one working policy, one working test that proves it, rather than an exhaustive matrix.
- **Every phase ends by filling in its section of `DEMO.md`**, the classroom runbook: the story, the concept, every command in order with what it does and what you should see, what to show, what to break, the students' vibe-coding prompt and the checkpoint. Only commands that were actually run are listed. A phase without its `DEMO.md` section is not done.

### 0.3 Open source only (except LLM inference)
- **LLM inference:** the Groq API (`GROQ_API_KEY` in `.env`) serving **open-weight models**. It must be swappable through config to a self-hosted vLLM or Ollama.
- **Agent framework:** **LangGraph** with the Postgres checkpointer. Tools are exposed over **MCP** (`mcp` + `langchain-mcp-adapters`).
- **Everything else is self-hosted open source:**
  - Data and files: PostgreSQL + pgvector, MinIO.
  - Identity, policy and gateways: Keycloak (identity), OPA (policy decisions), Envoy (ingress and egress gateway), LiteLLM Proxy (AI gateway, using the OSS features only).
  - Observability: Langfuse (LLM tracing), Prometheus and Grafana.
  - Document and file handling: ClamAV, Tesseract/OCRmyPDF, PyMuPDF, ezdxf, CadQuery.
  - ML and language: fastembed (local embeddings), scikit-learn, lingua-py, Presidio.
  - App and output: WeasyPrint, FastAPI + HTMX.

### 0.4 Models: verify first, then pin
In S1, call `GET https://api.groq.com/openai/v1/models` and pin the IDs that **actually exist** into `config/models.yaml`. Expose them as LiteLLM aliases. Starting candidates:

| Alias | Candidate model | Use |
|---|---|---|
| `quote-reasoner` | `openai/gpt-oss-120b` | extraction, reconciliation, drafting |
| `quote-vision` | `meta-llama/llama-4-scout-17b-16e-instruct` (or the current Groq vision model) | scanned drawings |
| `quote-triage` | `llama-3.1-8b-instant` | classification |
| `quote-guard` | `meta-llama/llama-prompt-guard-2-86m` | injection detection |
| `quote-safety` | `meta-llama/llama-guard-4-12b` | output safety |
| (local) | `BAAI/bge-small-en-v1.5` via fastembed | embeddings; this never leaves the box |

### 0.5 Golden rules of this agent
1. **The LLM reads, retrieves, explains and writes. Code calculates. Humans approve.** Prices, dates, CO₂ figures and validity terms come **only** from deterministic code.
2. **Every number has a source:** an Odoo record, a rate, a model version, a policy rule or an emission-factor citation.
3. **Nothing goes to a customer or supplier without approval** (a human approval, or an explicit OPA policy allowing it).
4. **Every AI call goes through LiteLLM.** It is the only holder of the Groq key, and each agent uses its own virtual key and budget.
5. **Every permission decision is made by OPA**, never hardcoded in the app, and it **fails closed** (if OPA can't be reached, the answer is no).

---

## 1. THE ENGAGEMENT: phases grouped into milestones

| Milestone | Phases | Outcome for the client |
|---|---|---|
| **M1 Discovery & design** | S0 | A shared understanding, an agreed scope, success metrics and an architecture |
| **M2 Pilot slice** *(core)* | S1–S8 | A real RFQ becomes a costed quote with a delivery date and CO₂, approved by a human and sent. **This is the first demo to Meera.** |
| **M3 Make it trustworthy** *(core)* | S9–S10 | Governance, security, cost control, evals and dashboards |
| **M4 More value** *(extension)* | S11–S18 | The remaining weaknesses fixed, one feature per phase |
| **M5 Go live & hand over** *(core)* | S19–S20 | Production readiness, shadow → assisted rollout, results, handover |

If time runs short, **M1 + M2 + M3 + M5 is a complete project.** The M4 phases are independent extensions.

---

## 2. ARCHITECTURE
```
          ┌──────────── CLIENT'S LEGACY SYSTEM (kept, not replaced) ────────────┐
          │  docker-mailserver + Roundcube     Odoo 17      Samba file share     │
          └────────┬──────────────────────────────┬──────────────────┬──────────┘
              IMAP/SMTP                       JSON-RPC            SMB (read)
                   │                              │                  │
   ingest-worker ──┘                  erp-mcp ────┘     share-mcp ───┘   (MCP tool servers, scoped creds)
        │  → MinIO (raw files), Postgres job queue
        ▼
   agent-worker: LangGraph StateGraph + Postgres checkpointer
     security_gate → triage → extract → reconcile → retrieve → cost/leadtime/CO₂
     → risk_policy (OPA) → draft → human_review (interrupt) → act → learn
        │ every LLM call ─▶ LiteLLM proxy ─▶ Envoy egress ─▶ api.groq.com (only allowed host*)
        │ every permission ─▶ OPA
        ▼
   review-ui (FastAPI+HTMX) ◀── Envoy ingress (TLS, JWT from Keycloak, rate limit, ext_authz→OPA)
   Observability: Langfuse · Prometheus · Grafana       Audit: hash-chained log in Postgres
   (*plus the commodity-index data host added in S5)
```
Record each decision as a short ADR in `docs/adr/`: a graph rather than a free-roaming agent; a Postgres queue rather than a separate broker; MCP for least privilege; OPA for decisions with YAML holding the data; LiteLLM as the only AI gateway; Envoy egress to prevent data leaks.

## 3. REPO LAYOUT (agent part)
```
agent/
├── config/ models.yaml  policy.yaml  agents.yaml (AI agent registry)  emission_factors.yaml
├── policy/rego/          # OPA rules + tests
├── ops/ envoy/ litellm/ keycloak-realm.json grafana/ prometheus/
├── src/apex_agent/
│   ├── settings.py  llm/client.py (→ LiteLLM)  audit/
│   ├── ingest/  security/  documents/  domain/ (Pydantic models)
│   ├── knowledge/ (sync, normalize, embed, search, commodity_index)
│   ├── costing/ (engine, leadtime, co2, terms, cycle_time_model, dfm, win_model, capacity)
│   ├── graph/ (state, nodes/, subgraphs/, build)
│   ├── actions/ (odoo_quote, quote_pdf, smtp_reply, supplier_rfq)
│   └── api/ (review UI)
├── mcp_servers/ erp_mcp/ share_mcp/ docs_mcp/
├── evals/ datasets/ run_evals.py thresholds.yaml
└── tests/ integration/ e2e/ security/ unit/ (pure functions only)
docker-compose.agent.yml   # layered on top of docker-compose.legacy.yml
```

---

## 4. PHASES

### M1 — Discovery & design

#### Phase S0 — Discovery and solution design (documents, no code)
**Fixes:** nothing yet. This phase earns the right to fix things.
- `docs/fde/DISCOVERY.md`: stakeholder map (the personas from file 01 §1.2), interview notes (role-played in class), the current-state flow, and the **W1–W18 list** with evidence from `docs/legacy/`.
- `docs/fde/SOLUTION_DESIGN.md`: for each weakness, the fix, the phase that delivers it, and the success metric, measured against `BASELINE_METRICS.md`. Also: what we will **not** do (we will not replace Odoo, auto-price without a human or auto-send).
- `docs/fde/SOW.md`: a short scope of work covering milestones M1–M5, assumptions, the client responsibilities (data access, a reviewer's time) and the acceptance criteria.
- `docs/security/THREAT_MODEL.md` v1: STRIDE plus the **OWASP Top 10 for LLM Applications**, a short version that is extended in S9.
- `docs/ARCHITECTURE.md` and the ADRs listed in §2.
- **Done when:** the documents exist and the human approves the scope.

### M2 — Pilot slice (core)

#### Phase S1 — Platform foundations
**Fixes:** groundwork for W3 and W16.
- `docker-compose.agent.yml` adds: `app-postgres` (pgvector), `minio`, `keycloak`, `opa`, `litellm` (with its database in app-postgres) plus `redis`, `envoy-ingress`, `envoy-egress`, `langfuse` (with the DB and ClickHouse its self-hosting docs require), `prometheus`, `grafana` and `clamav`. Containers run as non-root, images are pinned, and every service has a healthcheck.
- **Networks:** `edge`, `app`, `data`, `legacy` and `egress`. The app networks are `internal: true`. **Only `litellm` (and, later, the commodity-index fetcher) sits on `egress`**, and `envoy-egress` allows `api.groq.com` only.
- LiteLLM: model aliases from `config/models.yaml`, a fallback chain that ends at "escalate to a human" (never an unvetted model), timeouts and retries, and the Langfuse callback. `ops/litellm/keys.py` creates **one virtual key per agent** from `config/agents.yaml`, with its allowed aliases, RPM/TPM limits and budget.
- `settings.py` fails fast on missing config. `llm/client.py` points at LiteLLM.
- **Done when:**
  - a real Groq completion through LiteLLM shows up in Langfuse with a $ cost;
  - the triage key is **refused** when it asks for the reasoner model;
  - the agent container **cannot** reach the internet directly;
  - `gitleaks` is clean.

#### Phase S2 — Secure integration and the knowledge layer
**Fixes:** W2 (scattered data), W3 (shared login), W4 (dirty data), W5 (stale and Excel-only prices), W6 (rules in Ramesh's head).
- **Scoped access, replacing the shared `office` login:**
  - Odoo users `erp_reader` (read-only) and `erp_integration` (sales/purchase user rights), created by script;
  - a dedicated IMAP user `agent@` with access to `rfq@`;
  - a read-only SMB user.
- **MCP servers:**
  - `erp-mcp` has a read tool set and a separate write tool set, each with its own credential;
  - `share-mcp` is read-only;
  - each tool checks OPA (`tools.rego`: which agent node may call which tool).
- **Read model:** `knowledge/sync_odoo.py` does an incremental, idempotent sync (using `write_date` watermarks) of products, BOMs, routings, orders, MOs, suppliers and stock into app-postgres. **Odoo stays the system of record.**
- **Data cleanup in the read model:**
  - `normalize.py` maps material name variants to one canonical grade (a lookup table plus fuzzy matching, with a human-reviewed mapping file) and flags duplicate customers;
  - products missing a routing are flagged and estimated from analogs, with a visible "estimated" marker;
  - **write nothing back to Odoo without approval.** The data-quality findings go into a report for the client: `docs/fde/DATA_QUALITY.md`.
- `sync_share.py` parses the old quote sheets (marking the Excel-only quotes) and the **"Ramesh notes"** sheet. The LLM proposes structured rules; a human approves them into `config/policy.yaml` with a rule ID. Where the stale price list conflicts with Odoo, **Odoo wins** and the conflict is logged.
- Embeddings and hybrid search (`search.py`): structured filters, Postgres full-text search and pgvector, returning the top-k analogs with their actual times and won or lost prices.
- **Done when:**
  - the shared login is no longer used by the agent;
  - `erp_reader` is refused when it tries to write;
  - the sync is idempotent;
  - the data-quality report lists the seeded duplicates and name variants;
  - the top-5 analogs contain a same-family, same-material part for most held-out parts (report the %).

#### Phase S3 — Ingestion, security gate and triage
**Fixes:** W1 (start of automation) and the risk of untrusted email.
- **IMAP ingestion** with IDLE plus a polling fallback. Ingested mail is marked with the `$ApexIngested` keyword and is never moved or deleted, so the humans' inbox is unchanged.
  - Raw `.eml` files and attachments go to MinIO under their sha256 hash.
  - Duplicates are detected by Message-ID plus attachment hash.
  - Threads are detected from `In-Reply-To`/`References`.
  - Each new message becomes a `jobs` row (a Postgres `SKIP LOCKED` queue).
- **File safety:** a ClamAV scan, the real file type checked with libmagic, and ZIP limits (depth, file count and size).
- **The security gate:**
  - strip hidden text and zero-width characters;
  - run Prompt Guard through LiteLLM;
  - pass untrusted content only inside delimited `<untrusted_document>` blocks;
  - check the sender with SPF/DKIM/DMARC and against the customers in Odoo;
  - redact personal data with Presidio **in logs and traces only**.
  - **A flagged job is still processed, but it gets a red banner and requires a human.**
- **Triage** with the `quote-triage` model and structured output: `rfq | rfq_followup | supplier_reply | not_rfq | unclear`.
- **Done when:**
  - the labelled traffic produces one job per email, with no duplicates on re-run;
  - the EICAR test file is quarantined;
  - triage and injection-detection accuracy on the labels are reported.

#### Phase S4 — Document understanding
**Fixes:** W1 (reading drawings by hand).
- An extraction subgraph uses the most exact source available:
  - **DXF** (ezdxf: dimensions, callouts and title block);
  - **STEP** (CadQuery: bounding box, volume and hole count);
  - **vector PDF** (PyMuPDF text with positions, plus regex for `Ø`, `M6×1.0`, `±`, ISO 2768 and Ra);
  - **scans** (OCRmyPDF plus `quote-vision`).
- The LLM merges these sources into a `PartSpec` (Pydantic structured output). **Every field has a `source` and a `confidence`.**
- **Multilingual:** detect the language with lingua-py, extract into an English spec, and keep `reply_language`.
- `RfqRequirements`: quantities and price breaks, the requested date, certificates, packaging, Incoterms, **whether CO₂ data was requested**, and **export-control markers**. An export-control marker means OPA forces escalation to a senior.
- **Reconcile and critic:** deterministic checks first (units, material known, tolerance plausible), then an LLM critic. At most 2 retries, after which the field is marked `needs_human`. **Missing critical information** takes the clarification branch (an email to the customer, which needs approval).
- **Done when:** per-field accuracy against the L3 ground truth is reported (material, dimensions, holes, tolerances), plus the results on the NIST drawings.

#### Phase S5 — Cost, delivery date, CO₂ and quote terms (deterministic)
**Fixes:** W5 (stale prices), W7 (delivery dates by gut feel), W8 (no validity or price protection), W15 (no CO₂ answer).
- **Cost** (`engine.py`):
  - live rates and prices from Odoo through `erp-mcp`;
  - a cycle-time model (scikit-learn GradientBoosting with P10/P50/P90 quantiles, trained on actual times from completed MOs, with a model card stored in MinIO);
  - a `CostBreakdown` covering material, setup, run time per operation, inspection, certificates and outside processes, then overhead, then margin by policy, then price breaks;
  - **every line has its provenance.**
  - Compare the result with the prices of the analogs; a large gap raises a risk flag.
- **Delivery date** (`leadtime.py`):
  - material readiness, from stock or the vendor's lead time;
  - greedy forward-loading of the routing operations into the free capacity of each work center, using the open work orders and the calendars;
  - outside-process lead times and the policy buffer.
  - The output is a P50 date, a **committed date** (based on the P90 cycle time), a rush option (overtime plus a premium) and the **name of the bottleneck**.
  - If the customer's requested date isn't feasible, raise a risk flag and state the earliest feasible date.
  - `# ponytail:` greedy scheduling; upgrade to OR-Tools CP-SAT if the backtest falls short.
- **CO₂ footprint** (`co2.py`):
  - material weight (stock weight, including scrap) × a cradle-to-gate emission factor;
  - plus machine hours × `x_power_kw` × a grid emission factor;
  - plus outside processes (a factor per process);
  - plus optional transport.
  - The factors live in `config/emission_factors.yaml`, **each with its source, year and units**. Use published open sources such as industry-association averages and Ember for the grid, and verify them at build time.
  - The output is kg CO₂e per part and per lot, with a methodology note: *"an estimate following the GHG Protocol product-level approach, not a certified product carbon footprint."*
  - It is included in the quote when the customer requested it, and is otherwise available to the reviewer.
- **Quote terms** (`terms.py`):
  - a validity date (from policy, shorter for volatile materials), written to Odoo's native `validity_date`;
  - a **material price-protection clause**. `knowledge/commodity_index.py` fetches a real public monthly commodity price series (for example the World Bank "Pink Sheet"; verify the source and URL, and add that host to the Envoy egress allowlist). The index value on the quote date is stored. The clause reads: *"if the index moves more than X % before acceptance, the material portion is adjusted proportionally."*
- **Done when:**
  - the median absolute % error against held-out history is reported (target about 12 %);
  - the committed date is met in most historical cases in a backtest (report the %);
  - a `hypothesis` test proves that a tighter tolerance never lowers the price and that a higher quantity never raises the unit price (unless a policy says so);
  - CO₂ is reproducible by hand for one part;
  - the commodity index is fetched for real and stored.

#### Phase S6 — LangGraph orchestration
**Fixes:** W1 (the end-to-end flow).
- A typed `state.py`. The nodes are `security_gate → triage → extract → reconcile → retrieve → cost_leadtime_co2_terms → risk_policy (OPA must_escalate) → draft_reply → human_review (interrupt) → act → learn`, with conditional edges for `not_rfq`, `clarify`, `escalate` and `no_bid` (the last is added in S11). Multi-part RFQs use `Send` to fan out and then join.
- `PostgresSaver` checkpointing; `interrupt()` and `Command(resume=…)` for human review.
- Guards: a timeout per node, a maximum number of retries, the recursion limit, a **per-job $ budget** (summed from LiteLLM's response cost header, escalating to a human when exceeded) and idempotency keys.
- `draft_reply` writes in the customer's language plus an English copy for the reviewer. A **faithfulness check** verifies that every number in the draft exists in the breakdown, dates or CO₂ figures; if not, the node fails.
- Design the state so that later phases add nodes without a rewrite.
- **Done when:** a real RFQ runs to the interrupt; the worker is killed and restarted, and the job resumes; the full trace is visible in Langfuse.

#### Phase S7 — Review UI and access control
**Fixes:** W3 (who did what), W1 (a fast, focused review).
- FastAPI + HTMX behind Envoy, with Keycloak OIDC.
- Roles:
  - `estimator`: approves up to the limit;
  - `senior_estimator`: approves above the limit and edits policy;
  - `planner`: sees delivery dates and capacity;
  - `auditor`: read-only;
  - `admin`: **cannot approve**.
- Authorization: Envoy `ext_authz` → OPA checks each route; the app asks OPA `allow_approve` for each action.
- Screens:
  - a queue;
  - a job view with the drawing (PDF.js) beside the spec, showing each field's source and confidence, editable;
  - the analogs;
  - the cost, delivery-date and CO₂ breakdown with provenance links;
  - the risk flags and the draft;
  - the actions **Approve / Edit & approve / Clarify / Reject**, each with a reason.
- Every edit is stored as a diff (field, old, new, user, reason).
- **Done when:** a Playwright test with a real Keycloak login shows an estimator approving a small quote, being blocked on a large one, and a senior approving the large one.

#### Phase S8 — Actions and closing the loop
**Fixes:** W1 (reply the same day), W3 (traceable actions).
- After an approval token has been verified by OPA `allow_action`:
  - create a **draft** `sale.order` in Odoo (as `erp_integration`, with an idempotency key), with the lines, price breaks, `validity_date`, the terms and the attached drawing;
  - render the quote PDF with WeasyPrint, including the price, delivery dates, CO₂ (if requested), validity and price-protection clause, and the assumptions;
  - send the reply from `rfq@` with the correct threading headers;
  - set `x_quote_sent_at`.
- `learn`: record the outcome when an Odoo order is confirmed or cancelled. A new model is promoted only if it beats the current one on held-out data, and a human approves the promotion.
- **Done when (the pilot demo):** a customer emails an RFQ; a costed draft with a delivery date appears in the UI; an estimator approves it; the customer's mailbox receives the threaded quote PDF; the draft quotation is in Odoo.

### M3 — Make it trustworthy (core)

#### Phase S9 — Governance, security and cost control
**Fixes:** W3, W16 and W18, and the risks the AI itself introduces.
- **OPA policies with tests** (`opa test`, part of `make check`):
  - `approval.rego`, `escalation.rego` (returns reasons: value, margin floor, new customer, low confidence, injection flag, export control, an infeasible date);
  - `actions.rego` (send email, create a quote, contact a supplier);
  - `tools.rego`.
  - The data comes from `config/policy.yaml`.
  - OPA decision logs are kept and referenced from the audit log.
- **Audit log:** append-only and hash-chained (`hash = sha256(prev_hash + row)`); the app's database role has INSERT only; a `verify_audit_chain` command checks it. It records LLM calls (with redacted content), tool calls, OPA decisions, human decisions and outbound actions.
- **AI governance:**
  - **Agent registry** (`config/agents.yaml`): owner, purpose, risk level, model aliases, tools and budget for each agent. `make check` fails if code uses anything unregistered.
  - Prompt versions are kept in Langfuse; the `production` label moves only after the evals pass and a reviewer signs off.
  - Model swaps must pass the eval gate.
  - Guardrails run at LiteLLM: Presidio masking, Prompt Guard and Llama Guard. If a hook turns out to be enterprise-only, implement it as a custom callback.
  - **Kill switch:** `ai_enabled: false` makes OPA deny every AI step and the LiteLLM keys get blocked, so the system drops into manual mode. Test it.
- **Cost governance:**
  - budgets per call (`max_tokens`), per job, per agent per day and per month (a LiteLLM team budget), with alerts at 50/80/100 %;
  - model tiering (the cheapest model that passes the eval);
  - a Redis cache for identical deterministic calls;
  - skip the vision call when DXF or STEP geometry exists;
  - tag every call with metadata (job, agent, customer);
  - report **AI $ per quote, $ per won order and ROI** (engineer hours saved × rate, compared with AI spend);
  - a loop-protection test.
- **Human oversight:** track approval time and edit rate per reviewer, and inject **canary** (known-bad) drafts at random to detect rubber-stamping.
- **Data governance:**
  - a data-flow diagram showing what leaves the box (prompts sent to Groq through LiteLLM) and what doesn't;
  - retention jobs;
  - only the necessary pages are sent to the LLM;
  - an option in policy to keep export-controlled jobs off external LLMs.
- **Security fixes for the client:** a scoped, read-only share user for the agent, plus a recommendation report (`docs/fde/SECURITY_RECOMMENDATIONS.md`) for Vikram on the legacy share and the shared logins.
- **Red-team suite** (`tests/security/`): injection in the email body, the PDF hidden layer and DXF text; a "quote $1" email; requests for the system prompt or for other customers' prices; SSRF through a link; an oversized PDF; a jailbreak of the what-if assistant (once S16 exists). **Every attack must fail.**
- Supply chain, run locally in `make check`: `pip-audit`, `bandit`, `trivy` and an SBOM (`syft`).
- `docs/governance/`: model cards, an AI risk register mapped to **NIST AI RMF** and **ISO/IEC 42001**, a human-oversight statement and an AI incident runbook.
- **Done when:** the red-team suite is green, the audit chain verifies, the kill switch has been tested, the budgets are enforced, and the OPA tests pass.

#### Phase S10 — Observability and evaluation
**Fixes:** W17 (no metrics).
- Langfuse: traces, prompt versions and datasets (built from the ground truth and the human edits).
- Prometheus and Grafana dashboards:
  - **Operations** (queue, latency, errors);
  - **Business KPIs, before and after** compared with the L6 baseline (response time, win rate, on-time delivery);
  - **AI Quality**;
  - **AI Cost & Governance**;
  - **Human Oversight**.
- Basic alerts: queue age, Groq errors, budgets.
- `evals/run_evals.py` covers triage, extraction, retrieval, costing, delivery date, draft faithfulness and red-team. `thresholds.yaml` holds the targets, and **`make check` fails on a regression** compared with the last recorded scores.
- **Done when:** the dashboards show live data from a `--drip` run, and a deliberately broken prompt fails `make check`.

### M4 — More value (extensions; each one independent)

#### Phase S11 — Capability check, bid/no-bid and queue priority
**Fixes:** W11 (quoting what we can't make), W1 (the FIFO inbox).
- A deterministic `capability_check` against the machine envelopes, best tolerance and finish, materials, processes and certificates. It returns `bid`, `bid_with_conditions` or `no_bid` with the reasons. A `no_bid` produces a polite decline draft, which needs approval.
- `prioritize`: P(win) × expected value × capacity fit × urgency. Use the analog win rate as P(win) until S14 exists. The queue is sorted by this score, and its factors are shown.
- **Done when:** the out-of-capability RFQs are caught, no capable part gets a no-bid, and the queue order is explained on screen.

#### Phase S12 — Repeat parts and revisions
**Fixes:** W10 (re-quoting from scratch).
- `repeat_detect` before extraction checks, in order: the file hash, the customer and part number, a geometric fingerprint, then embedding similarity.
  - A **repeat** reuses the approved spec and reprices it at today's costs, showing the old and new price with the reasons.
  - A **revision** is diffed: entities for DXF; text plus a rendered pixel diff for PDF (PyMuPDF + numpy). The changes are highlighted, the LLM summarizes them, and only the changes are repriced.
- **Done when:** the labelled repeats and revisions are detected, with no false matches across different customers.

#### Phase S13 — DFM suggestions
**Fixes:** W12 (no cost-down advice).
- Rules defined in policy: small internal radii, deep holes, thin walls, tolerances tighter than needed, finish finer than needed, non-standard threads, exotic materials.
- **Re-run the engine** to quantify each saving. The suggestions appear only as **optional alternate lines**; the customer's spec is never changed.
- **Done when:** the seeded DFM issues are found, and every saving can be reproduced by the engine.

#### Phase S14 — Win probability, margin guidance and portfolio capacity
**Fixes:** W6 (pricing by gut feel), W7 (promising the same machine time twice).
- `win_model.py`: a calibrated classifier trained on won and lost history, with the data split by time; report the Brier score and a calibration plot; include a model card.
- The UI shows a curve of **margin against P(win) against expected profit**. It is advisory only; OPA enforces the margin floor and ceiling. S11's `prioritize` switches to this P(win).
- **Portfolio capacity** (`capacity.py`): the expected load = confirmed MOs + Σ(open quotes × P(win)), per work center per week. It is shown as a heatmap in the UI. The `leadtime` calculation for new quotes includes the expected load (using a weighting from policy), and **an over-commitment risk flag** is raised when the expected load exceeds capacity.
- **Done when:** the model beats the "average win rate" baseline on the time split; the heatmap shows the seeded load; flooding the system with open quotes visibly pushes the committed dates out.

#### Phase S15 — Follow-ups, replies, quote expiry and the digest
**Fixes:** W8 (late acceptances at old prices), W9 (no follow-ups or lost reasons), W17 (no management view).
- Scheduled jobs on the same queue (with a `due_at`): a follow-up draft N days after sending (approved by a human, or allowed by OPA for plain reminders).
- `reply_router` classifies replies in a thread:
  - `accept`: a human confirms the order in Odoo; the agent never confirms orders itself;
  - `reject(reason)`: write the lost reason to Odoo;
  - `negotiate`: a task for the reviewer with the S14 curve, if it exists;
  - `question`: draft an answer;
  - `revision`: re-run the pipeline.
- **Expiry and price protection:** an acceptance after `validity_date`, or an index movement beyond X %, triggers an automatic reprice and a draft notice to the customer for a human to review. Expired quotes are marked in Odoo.
- **Weekly digest** to management: the KPIs come from SQL, the LLM writes the narrative, and a faithfulness check runs on every number.
- **Done when:** the `customer_responder` replies are routed correctly; lost reasons appear in Odoo; a late acceptance triggers a reprice; the digest passes the faithfulness check.

#### Phase S16 — What-if assistant
**Fixes:** W6 (only Ramesh can answer pushback).
- A chat panel in the job view: a LangGraph ReAct agent with **read-only tools** (`recompute_cost`, `recompute_leadtime`, `recompute_co2`, `explain_line_item`, `get_analogs`, `get_policy_rule`, `win_curve`). It has its own virtual key and a budget per session.
- Every number comes from a tool, with citations. It cannot approve, edit or send. "Apply as option" is a human click that is recorded in the audit log.
- **Done when:** a scripted set of what-if questions gets tool-sourced answers, and injection attempts can't trigger any action.

#### Phase S17 — Supplier RFQ automation
**Fixes:** W13 (stale supplier prices), W18 (no confidentiality on drawings).
- If material is out of stock, or an outside-process price is stale, draft RFQs to 2–3 vendors from Odoo. **Share only what's needed.** If a drawing is required, redact the title block and the customer name with PyMuPDF. OPA **blocks** the send for NDA-restricted customers or export-controlled jobs.
- The send is approved by default. The subgraph waits through a checkpoint with a timeout. Supplier replies are extracted into a `SupplierQuote` with provenance; the best one feeds cost and lead time, and a draft `purchase.order` is created in Odoo. If the timeout passes, use the last known price plus a risk flag.
- **Done when:** the `supplier_responder` replies are extracted; the redaction is verified (the customer name never leaves); the NDA and export-control blocks are tested.

#### Phase S18 — Quoted versus actual, and override mining
**Fixes:** W14 (nobody knows which jobs lose money), W6 (Ramesh's knowledge never written down).
- Compare the actual MO times, material and scrap with the quote, per operation. A dashboard shows the variance by family, machine, material and customer, plus **the jobs that lost money**. Retrain the cycle-time model on the actual times through the promotion gate.
- **Override mining:** group the human edits and their reasons into patterns. The LLM proposes a candidate policy rule with the evidence, and a senior estimator approves or rejects it. Approved rules go into `policy.yaml`, and the evals are re-run.
- **Done when:** the variance dashboard is live, and at least one seeded override pattern is mined, approved and shown changing the next quote.

### M5 — Go live & hand over (core)

#### Phase S19 — Production readiness (prototype level, local only)
**Fixes:** W16 (no backups or monitoring).
- **Everything runs locally** on the developer's machine with Docker Compose. There is no cloud, no remote server, no hosted CI and no deployment pipeline.
- **Local quality gate:** `make check` runs lint, type-check, tests, evals, `opa test` and the security scans (`pip-audit`, `bandit`, `gitleaks`, `trivy` on the locally built images). A **git pre-commit hook** (the `pre-commit` framework) runs the fast checks on every commit. `make check` is what "the gate" means everywhere in these specs.
- Backups: `pg_dump` of both Postgres instances plus a MinIO mirror, with **a scripted restore drill that has actually been run**.
- Scripted resilience drills:
  - Groq down: jobs queue, the UI shows "manual mode", nothing is lost;
  - Odoo down;
  - the worker killed mid-job;
  - a burst of 100 RFQs.
- `docs/runbooks/`: start and stop the local stack, roll back to a previous image or tag, key rotation, a stuck job, a suspected injection, the kill switch and model rollback.
- **Done when:** the drills have been run and their results recorded, and a restore has worked.

#### Phase S20 — Rollout, results and handover
**Fixes:** proves that W1–W18 have been addressed.
- **Shadow mode:** the agent drafts but sends nothing, and the humans quote as before. `scripts/shadow_report.py` compares the agent's drafts with the human quotes.
- **Assisted mode:** the agent drafts, a human approves, and the agent sends.
- `docs/RESULTS.md`: before and after compared with `BASELINE_METRICS.md` (response time, minutes per quote, on-time delivery, win rate, AI cost per quote), plus the W1–W18 status table (fixed, partially fixed, or not fixed) and **honest failure cases**.
- **Handover:** `docs/fde/HANDOVER.md` covers what was built, how to operate it, who owns what at Apex, known limits, and the recommended next steps.
- **Finish `DEMO.md`:**
  - write the **★ Capstone** section, one continuous before/after script that has been tested end to end:
    1. `make demo-reset && make demo-legacy`, then quote one RFQ by hand with a timer running;
    2. `make demo-agent`, then the same kind of RFQ produces a draft within minutes, which is approved;
    3. show the trace, the audit log, the cost per quote and the before/after dashboard;
    4. show the W1–W18 table and the handover.
  - Then **do a full dry run of the whole `DEMO.md` from a fresh `make demo-reset`**, top to bottom, and fix every step that doesn't match reality.
- **Done when:** the live demo runs from beginning to end, and the handover pack is complete.

---

## 5. FINAL DEFINITION OF DONE
- [ ] Zero mocks; every test runs against real services and the real Groq API.
- [ ] Quotes answer **price, delivery date, CO₂ (on request) and terms**, all calculated by code with provenance.
- [ ] Nothing goes out without approval; OPA enforces the policies and fails closed; separation of duties holds.
- [ ] Every AI call goes through LiteLLM with registered keys and budgets; the kill switch has been tested.
- [ ] Red-team suite green; audit chain verified; eval gate in `make check`; dashboards live.
- [ ] Everything runs locally with Docker Compose; there is no cloud or hosted deployment.
- [ ] `DEMO.md` is complete for every phase plus the capstone, and has been dry-run from a fresh reset.
- [ ] `RESULTS.md` compares honestly against the baseline, with the W1–W18 status; the handover pack is done.
