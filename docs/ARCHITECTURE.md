# Architecture: the Apex quoting agent

> **Status:** v1.10 (S10, 2026-11-22): + evaluation (`agent/evals/`: run_evals, thresholds, recorded scores; a 3-email live golden triage set through the cache; scores to Langfuse and `eval_runs`; in `make check`), Langfuse datasets, five generated Grafana dashboards (Operations, Business KPIs before/after, AI Quality, AI Cost & Governance, Human Oversight), three provisioned alerts; the red-team suite is opt-in (`make redteam`). v1.9 (S9, 2026-11-22): + governance: hash-chained, append-only `audit_log` (trigger-computed; writers use the INSERT-only role `apex_audit`; `verify_audit_chain`); OPA query-only (`system.authz`), decision logs with masking, `escalation.rego` and `data.rego`; the kill switch (`make ai-off/ai-on`: OPA + LiteLLM key block); LiteLLM guardrails callback (registry max_tokens, PII masking, drafter output safety via `quote-safety`) and Redis cache; budget alerts; canaries; retention; export-controlled jobs can stay local; prompts and the model list versioned in Langfuse; supply-chain checks in `make check`; `docs/governance/`. > **Status:** v1.8 (S8, 2026-11-22): + `mail-mcp` (send_reply as rfq@ via the agent's Dovecot master login; threaded; idempotent by Message-ID; copy to Sent); `erp-mcp-write` creates the draft quotation (options for price breaks, validity, terms, drawings) and `mark_quote_sent`; every write tool asks OPA `apex/actions` with the review screen's signed approval token (HS256 key held only by the screen and OPA) and hashes of the exact email/prices; `act` renders the quote PDF (WeasyPrint); `actions`/`outcomes` tables; learn syncs won/lost from Odoo; cycle-time models are candidates until a person promotes a better one. v1.7 (S7, 2026-11-21): + `review-ui` (FastAPI + HTMX) at **:8448** behind Envoy (Keycloak oauth2 login, jwt_authn, ext_authz -> OPA `apex/ingress`, rate limit); OPA now runs with its Envoy plugin (gRPC :9191); decisions go through OPA `apex/review` in the app and in `QuoteGraph.resume`; the UI hands decisions to the worker via `jobs.decision`; `review_items`; staff logins arjun/ramesh/kavita/meera/vikram. v1.6 (S6, 2026-11-21): the worker now runs every job through the LangGraph quote graph (Postgres checkpoints, human-review interrupt); `graph_events`, `decisions`, `feedback`. v1.5 (S5, 2026-11-21): + the costing package (engine, cycle-time model in MinIO, delivery date, CO2, terms), the World Bank index through egress, `estimates` table. v1.4 (S4, 2026-11-21): + the extraction subgraph (LangGraph) in the worker, `extractions` table, Grafana extraction panels. v1.3 (S3, 2026-11-21): + `ingest` and `worker` services, the security gate, triage, the Grafana *Inbox triage* dashboard. v1.2 (S2, 2026-11-20): + MCP tool servers, scoped accounts, the knowledge read model. v1.1 (S1): the platform exists. Published UIs (all `127.0.0.1`, TLS from the lab CA): Langfuse **:8443**, Grafana **:8444**, Keycloak **:8445**, LiteLLM **:8446**, MinIO console **:8447**. Decisions are recorded as ADRs in [`docs/adr/`](adr/). Security view: [`THREAT_MODEL.md`](security/THREAT_MODEL.md). Each later phase updates this file when it adds a component.

## 1. Context
The agent is added **beside** the client's legacy system (built in L0–L6), not in place of it. It talks to the legacy systems only through their normal protocols and with scoped accounts.

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

## 2. Components
| Component | Technology | Responsibility | Phase |
|---|---|---|---|
| ingest (service `ingest`) | Python, imaplib (IDLE + 30 s polling) | Read rfq@ as the Dovecot master user `agent` with `BODY.PEEK` (the humans' read state is untouched), store raw mail and attachments in MinIO by sha256, one `jobs` row per Message-ID, threads from In-Reply-To/References, re-sent RFQs by attachment hash; marks only the `$ApexIngested` keyword, never moves or deletes mail | S3 |
| worker (service `worker`) | Python; ClamAV (clamd), libmagic, PyMuPDF, Prompt Guard 2 via LiteLLM, dkimpy, Presidio | Claims jobs with `SKIP LOCKED`; **security gate**: virus scan, real file type, ZIP limits, quarantine; hidden HTML/PDF text and zero-width characters stripped; Prompt Guard on every text; DKIM + DMARC alignment and the Odoo customer/display-name check; OPA decides the red banner. Then **triage** (`quote-triage`, structured output). A flagged email is still processed, with a red banner and a human. From S4, an `rfq` goes on to the **extraction subgraph** (below). Replaced by agent-worker's graph from S6 | S3, S4 |
| extraction subgraph (in `worker`) | **LangGraph** StateGraph; ezdxf (DXF), OpenCascade/OCP (STEP; CadQuery's kernel), PyMuPDF (vector PDF), OCRmyPDF + Tesseract and `quote-vision` (scans), lingua (language), `quote-reasoner` (the email), `quote-reasoner-lite` (critic) | Files the gate passed + the visible email → `PartSpec`s and `RfqRequirements`, **every field with source and confidence**. Code reads the drawings; the LLM reads only the email and must quote it word for word; dates are parsed by code. Deterministic checks, then a critic (max 2 rounds), then `needs_human` or questions for the customer. OPA routes export-controlled RFQs to a senior | S4 |
| quote graph (`apex_agent/graph/`, in `worker`) | **LangGraph** StateGraph + `PostgresSaver` | `security_gate → triage → extract → reconcile → retrieve → cost_part` (fan-out per part with `Send`, joined) `→ risk_policy` (OPA `apex/risk/decision`) `→ draft_reply` (drafter + faithfulness check) `→ human_review` (`interrupt()`, resumed by `Command(resume=…)`) `→ act → learn`. Checkpointed after every node: a killed worker takes its jobs back and resumes at the next node. Guards: step timeout, retries, recursion limit, per-job LLM budget, idempotent nodes | S6 |
| MCP tool servers | `mcp` (erp-mcp, share-mcp, docs-mcp) | The only way the agent touches Odoo and the share; each tool asks OPA first; read and write tool sets have separate credentials | S2 |
| Costing (`apex_agent/costing/`, in `worker`) | Python + scikit-learn | **Deterministic, no LLM.** `engine` (material, setup, run, inspection, documents, outside, overhead, margin by policy + approved rules, price breaks; every line with provenance), `cycle_time` (quantile gradient boosting on actual/standard minutes, monotonic, conformal P90; model + card in MinIO), `leadtime` (greedy forward-loading on the shift calendars against Odoo's open work; P50, committed, rush, bottleneck), `co2` (cited factors in `emission_factors.yaml`), `terms` (validity + index-linked price protection). Live facts via erp-mcp-read (node `cost_leadtime_co2_terms`), policy via OPA | S5 |
| review-ui (`apex_agent/api/`) | FastAPI + HTMX (vendored), the browser's PDF viewer, ezdxf SVG | Queue, job view (drawing beside the read fields with source and confidence, editable), analogs, cost/delivery/CO2 with provenance links, risk, draft; approve / edit & approve / re-cost / clarify / reject, each with a reason. Never runs the graph: writes `jobs.decision`, the worker resumes. Own Postgres role `review_ui` (UPDATE jobs(status, decision) only) and MinIO user `review-ui` (read raw/, files/) | S7 |
| app-postgres | PostgreSQL + pgvector | Read model of Odoo/share, job queue (`SKIP LOCKED`), checkpoints, embeddings, audit log | S1–S2 |
| MinIO | S3-compatible object store | Raw emails, attachments, model artefacts | S1 |
| LiteLLM proxy | LiteLLM (OSS features) | **Only** path to any LLM; model aliases, virtual key + budget per agent, fallbacks, guardrails | S1 |
| OPA | Open Policy Agent | Every permission decision (tools, approvals, actions, escalation); fails closed | S1, S9 |
| Keycloak | Keycloak | Named users and roles (estimator, senior_estimator, planner, auditor, admin) | S1, S7 |
| Envoy ingress / egress | Envoy | Ingress: TLS, JWT, rate limit, ext_authz → OPA. Egress: allow-list (`api.groq.com`, later the commodity index) | S1 |
| Langfuse, Prometheus, Grafana | OSS | LLM traces and cost, metrics, dashboards | S1, S10 |
| ClamAV | ClamAV | Malware scan of attachments | S1, S3 |
| knowledge job | Python (on demand / scheduled) | Odoo + share → read model (`odoo_records`, `parts`, `share_quotes`, `price_conflicts`…), local embeddings, `DATA_QUALITY.md`, rule proposals | S2 |
| Scoped accounts | Odoo `erp_reader` / `erp_integration`, Samba `agent-ro`, Dovecot master `agent` | Replace the shared `office` / share / rfq@ logins for everything the agent does (W3) | S2 |

## 3. Networks and trust boundaries
| Network | Members | Internet? |
|---|---|---|
| `edge` | envoy-ingress | Bound to 127.0.0.1 only |
| `app` (internal) | review-ui, agent-worker, worker, MCP servers, OPA, LiteLLM, Keycloak, Langfuse, ClamAV | No |
| `data` (internal) | app-postgres, MinIO, Redis; ingest, worker and Grafana (read-only role `grafana_ro`) as clients | No |
| `legacy` | the MCP servers and `ingest`, which need Odoo, mail, share | No |
| `egress` (internal) | **LiteLLM** and **ClamAV** (signature updates) ↔ envoy-egress; the knowledge job's World Bank download via `HTTPS_PROXY` | Only through envoy-egress, which allows `api.groq.com`, `database.clamav.net` and (S5) `thedocs.worldbank.org` |
| `outbound` | envoy-egress only: its own route to the internet | Yes, for the allow-listed hosts |

**Trust boundaries:** (1) customer and supplier email and attachments are **untrusted** until the security gate has processed them, and stay delimited as untrusted content in every prompt; (2) the LLM's output is untrusted. It can propose, but code validates it and OPA decides; (3) the edge, where humans log in through Keycloak; (4) egress, where data leaves the machine (prompts to Groq).

## 4. What leaves the machine
| Data | Leaves? | Where | Control |
|---|---|---|---|
| Prompts: relevant email text, extracted drawing text, page images of scans | **Yes** | Groq, through LiteLLM → Envoy egress | Only necessary pages; Presidio masks personal data in logs/traces; policy can keep export-controlled jobs local |
| Commodity index request | Yes (S5) | One public data host | Egress allow-list |
| ClamAV signature download | Yes (S1) | `database.clamav.net` (download only, nothing of ours is sent) | Egress allow-list |
| Everything else (Odoo data, prices, quotes, audit, embeddings) | **No** | — | Internal networks; embeddings computed locally (fastembed) |

## 5. Key flows
1. **RFQ → quote:** `SOLUTION_DESIGN.md` §5.
2. **Human review:** the graph calls `interrupt()`; the reviewer's decision (with reason and any edits as diffs) resumes it with `Command(resume=…)`; OPA `allow_approve` checks the role and the limits.
3. **Outbound action:** only after an approval token verified by OPA `allow_action`; each action is idempotent and written to the audit chain.

## 6. Quality attributes
- **Correctness:** numbers by code with provenance; faithfulness check on drafts; evals in `make check`.
- **Security:** least privilege (scoped accounts, MCP + OPA), fail closed, red-team suite, no new internet exposure.
- **Resilience:** queue + checkpoints survive restarts; Groq down → manual mode, nothing lost.
- **Cost:** per-call, per-job, per-agent budgets at LiteLLM; cheapest model that passes the eval.
- **Operability:** one `docker compose` stack layered on the legacy one; runbooks; backups with a tested restore.

## 7. Architecture decision records
| ADR | Decision |
|---|---|
| [0001](adr/0001-graph-not-free-agent.md) | A LangGraph state graph, not a free-roaming agent |
| [0002](adr/0002-postgres-queue.md) | A Postgres job queue, not a separate broker |
| [0003](adr/0003-mcp-least-privilege.md) | MCP tool servers for least-privilege access to legacy systems |
| [0004](adr/0004-opa-decisions-yaml-data.md) | OPA makes every permission decision; YAML holds the policy data |
| [0005](adr/0005-litellm-only-ai-gateway.md) | LiteLLM is the only AI gateway |
| [0006](adr/0006-envoy-egress.md) | Envoy egress allow-list to prevent data leaks |
| [0007](adr/0007-code-calculates.md) | Code calculates every number; the LLM never does |
