-- S3: ingestion, the job queue and the security gate's results (database apex, owner apex). Idempotent.

CREATE TABLE IF NOT EXISTS emails (
    id            bigserial PRIMARY KEY,
    message_id    text UNIQUE NOT NULL,          -- duplicate detection #1 (re-delivery, re-ingestion)
    raw_sha256    text NOT NULL,                 -- MinIO raw/<sha256>.eml
    mailbox       text NOT NULL,
    imap_uid      bigint,
    from_addr     text,
    from_name     text,
    subject       text,
    sent_at       timestamptz,
    in_reply_to   text,
    thread_id     bigint REFERENCES emails(id),  -- the first email of the conversation (In-Reply-To/References)
    duplicate_of  bigint REFERENCES emails(id),  -- duplicate detection #2: same sender + same attachment hashes
    ingested_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS attachments (
    id            bigserial PRIMARY KEY,
    email_id      bigint NOT NULL REFERENCES emails(id),
    filename      text NOT NULL,
    claimed_type  text,                          -- what the email says (Content-Type)
    sha256        text NOT NULL,                 -- MinIO incoming/, then files/ or quarantine/
    size          bigint NOT NULL,
    parent_id     bigint REFERENCES attachments(id),  -- a file found inside a ZIP
    true_type     text,                          -- libmagic, set by the gate
    verdict       text NOT NULL DEFAULT 'pending',    -- pending | clean | quarantined
    reasons       jsonb NOT NULL DEFAULT '[]',
    UNIQUE NULLS NOT DISTINCT (email_id, filename, sha256, parent_id)
);

-- The work queue (ADR 0002): workers claim with FOR UPDATE SKIP LOCKED.
CREATE TABLE IF NOT EXISTS jobs (
    id            bigserial PRIMARY KEY,
    email_id      bigint UNIQUE NOT NULL REFERENCES emails(id),  -- one job per email, ever
    status        text NOT NULL DEFAULT 'queued',  -- queued | running | triaged | failed
    attempts      int NOT NULL DEFAULT 0,
    available_at  timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz,
    error         text
);
CREATE INDEX IF NOT EXISTS jobs_queued ON jobs (available_at) WHERE status = 'queued';

CREATE TABLE IF NOT EXISTS gate_results (
    email_id          bigint PRIMARY KEY REFERENCES emails(id),
    injection_score   double precision,        -- max Prompt Guard probability over all text chunks
    hidden_text       jsonb NOT NULL,          -- what was stripped: hidden HTML, white PDF text, zero-width chars
    sender            jsonb NOT NULL,          -- DKIM, DMARC, SPF, known customer, display-name claim
    reasons           jsonb NOT NULL,          -- OPA's reasons for the red banner
    red_banner        boolean NOT NULL,
    needs_human       boolean NOT NULL,
    triage            text,                    -- rfq | rfq_followup | supplier_reply | not_rfq | unclear
    triage_confidence double precision,
    triage_reason     text,
    language          text,
    trace_id          text,                    -- Langfuse
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- S4: what was read from each RFQ (PartSpecs + RfqRequirements, every field with source and confidence)
CREATE TABLE IF NOT EXISTS extractions (
    email_id      bigint PRIMARY KEY REFERENCES emails(id),
    result        jsonb NOT NULL,
    parts         int NOT NULL,
    needs_human   int NOT NULL,           -- issues a person must look at
    clarification int NOT NULL,           -- questions for the customer (an email that needs approval)
    route         text NOT NULL,          -- OPA: estimator | senior (export control)
    language      text,
    llm_calls     int NOT NULL,
    trace_id      text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- S5: the deterministic estimate for each extracted RFQ part (cost, delivery date, CO2, terms; every line sourced)
CREATE TABLE IF NOT EXISTS estimates (
    email_id    bigint NOT NULL REFERENCES emails(id),
    part_code   text NOT NULL,
    result      jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (email_id, part_code)
);

-- S6: the quote graph. Checkpoints live in LangGraph's own tables (PostgresSaver.setup()); these are ours.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS locked_by text;   -- the worker (hostname) that claimed the job

CREATE TABLE IF NOT EXISTS graph_events (                  -- every finished node: proves a resumed job never re-runs one
    id         bigserial PRIMARY KEY,
    email_id   bigint NOT NULL REFERENCES emails(id),
    node       text NOT NULL,
    spent_usd  double precision NOT NULL DEFAULT 0,
    at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS graph_events_email ON graph_events (email_id, id);

CREATE TABLE IF NOT EXISTS decisions (                     -- the human's decision at review (S6 records it; S8 acts on it)
    email_id    bigint PRIMARY KEY REFERENCES emails(id),
    action      text NOT NULL,                             -- approve | edit | clarify | reject
    decided_by  text,
    reason      text,
    final_text  text,
    decided_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feedback (                      -- every human edit, field by field (S18 learns from these)
    id         bigserial PRIMARY KEY,
    email_id   bigint NOT NULL REFERENCES emails(id),
    field      text NOT NULL,
    old_value  jsonb,
    new_value  jsonb,
    by_user    text,
    reason     text,
    at         timestamptz NOT NULL DEFAULT now()
);

-- S7: the review screen. The UI never runs the graph: it writes the reviewer's decision here (status 'decided')
-- and the worker resumes the job with it. The UI's own role may update only these two columns.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS decision jsonb;  -- {action, by, name, roles, reason, edits}, cleared once applied

CREATE TABLE IF NOT EXISTS review_items (                  -- what human_review shows (the interrupt payload), one per job
    email_id     bigint PRIMARY KEY REFERENCES emails(id),
    route        text NOT NULL,                            -- quote | clarify | escalate
    value_usd    double precision NOT NULL DEFAULT 0,
    needs_senior boolean NOT NULL DEFAULT false,
    payload      jsonb NOT NULL,                           -- risk + draft (as the reviewer sees them)
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- S8: what act did after the approval (quotation name, Message-ID of the reply, PDF size), and the outcomes learn reads
-- back from Odoo later (the estimator confirms or cancels the quotation there).
CREATE TABLE IF NOT EXISTS actions (
    id         bigserial PRIMARY KEY,
    email_id   bigint NOT NULL REFERENCES emails(id),
    result     jsonb NOT NULL,
    at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outcomes (
    email_id     bigint PRIMARY KEY REFERENCES emails(id),
    quotation    text NOT NULL,                            -- the Odoo sale.order name
    state        text NOT NULL,                            -- Odoo: draft | sent | sale | cancel
    outcome      text NOT NULL,                            -- open | won | lost
    lost_reason  text,
    amount_usd   double precision,
    changed_at   timestamptz NOT NULL DEFAULT now()        -- when we first saw this state
);

-- S9 human oversight: synthetic known-bad drafts (governance/canary.py). NOT granted to the review screen's role.
CREATE TABLE IF NOT EXISTS canaries (
    email_id    bigint PRIMARY KEY REFERENCES emails(id),   -- the synthetic copy
    source_id   bigint NOT NULL REFERENCES emails(id),      -- the real job it was copied from
    wrong       text NOT NULL,                              -- the wrong price put into the draft
    right_price text NOT NULL,                              -- the computed one
    result      text,                                       -- caught | missed (after a decision)
    decided_by  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- S9 cost governance: an alert per agent, budget period and threshold (50/80/100 %), raised once.
CREATE TABLE IF NOT EXISTS budget_alerts (
    agent        text NOT NULL,
    period_start date NOT NULL,
    threshold    int NOT NULL,
    spend_usd    double precision NOT NULL,
    budget_usd   double precision NOT NULL,
    at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent, period_start, threshold)
);

-- S10: every evaluation run's scores (agent/evals/run_evals.py), for Grafana's "AI Quality" dashboard
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id   text NOT NULL,
    metric   text NOT NULL,
    value    double precision NOT NULL,
    passed   boolean NOT NULL,
    at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, metric)
);

-- S15: replies to quotes (reply_router; the classification is stored, so a re-run never repeats the LLM call), the
-- follow-up date of a sent quote, and the weekly digests
CREATE TABLE IF NOT EXISTS replies (
    email_id   bigint PRIMARY KEY REFERENCES emails(id),
    result     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS followup_at timestamptz;
CREATE TABLE IF NOT EXISTS digests (
    id         bigserial PRIMARY KEY,
    week       date NOT NULL,
    kpis       jsonb NOT NULL,
    narrative  text,
    faithful   boolean NOT NULL,
    unfaithful jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- S17: supplier RFQs, one row per (job, product, vendor): draft -> approved -> sent -> replied | timeout; or blocked
CREATE TABLE IF NOT EXISTS supplier_rfqs (
    id           bigserial PRIMARY KEY,
    email_id     bigint NOT NULL REFERENCES emails(id),
    code         text NOT NULL,
    vendor_ref   text NOT NULL,
    vendor_name  text NOT NULL,
    vendor_email text NOT NULL,
    need         jsonb NOT NULL,
    subject      text NOT NULL,
    body         text NOT NULL,
    attachment   text,                      -- MinIO key of the REDACTED drawing, if any
    redaction    jsonb,                     -- what was checked, and leaks (must be [])
    status       text NOT NULL,             -- draft | blocked | approved | sent | replied | timeout
    blocked      jsonb,
    supplier_ok  boolean NOT NULL,
    approval     text,
    approved_by  text,
    message_id   text,
    due_at       timestamptz,
    reply_email  bigint REFERENCES emails(id),
    quote        jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (email_id, code, vendor_ref)
);

-- S16: the what-if assistant's questions and answers (with every tool call: the citations), and the options a person
-- applied from an answer (the review screen writes those; the assistant can't)
CREATE TABLE IF NOT EXISTS whatif_log (
    id         bigserial PRIMARY KEY,
    email_id   bigint NOT NULL REFERENCES emails(id),
    session    text NOT NULL,
    by_user    text NOT NULL,
    question   text NOT NULL,
    answer     text NOT NULL,
    tools      jsonb NOT NULL,
    faithful   boolean NOT NULL,
    unfaithful jsonb NOT NULL,
    spent_usd  numeric NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS whatif_options (
    id         bigserial PRIMARY KEY,
    email_id   bigint NOT NULL REFERENCES emails(id),
    log_id     bigint NOT NULL REFERENCES whatif_log(id),
    part_code  text NOT NULL,
    changes    jsonb NOT NULL,
    qty        int NOT NULL,
    unit_price numeric NOT NULL,
    by_user    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (log_id, part_code, changes)
);

-- S14: the expected-load heatmap (costing/capacity.py), written by the graph and `python -m apex_agent.costing.capacity`
CREATE TABLE IF NOT EXISTS capacity_snapshots (
    id   bigserial PRIMARY KEY,
    at   timestamptz NOT NULL DEFAULT now(),
    data jsonb NOT NULL
);

-- S12: repeat / revision detection per email (costing/repeat.py); stored so a re-run never repeats the LLM summary
CREATE TABLE IF NOT EXISTS repeat_matches (
    email_id   bigint PRIMARY KEY REFERENCES emails(id),
    result     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- S18: quoted vs actual (costing/variance.py writes them; Grafana reads them through grafana_ro)
CREATE TABLE IF NOT EXISTS variance_jobs (
    sale_order text PRIMARY KEY, customer text, part text, family text, material text, qty numeric,
    revenue numeric, material_cost numeric, machine_cost numeric, actual_cost numeric, profit numeric, lost_money boolean,
    minutes_planned numeric, minutes_actual numeric, computed_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS variance_ops (
    id bigserial PRIMARY KEY, sale_order text, work_center text, family text, material text, customer text,
    minutes_planned numeric, minutes_actual numeric, ratio numeric);
