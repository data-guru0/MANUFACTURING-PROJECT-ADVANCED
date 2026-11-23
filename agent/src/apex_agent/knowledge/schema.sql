-- The agent's READ MODEL in app-postgres (S2). Odoo stays the system of record; nothing here is written back.
CREATE EXTENSION IF NOT EXISTS vector;

-- Raw copy of the Odoo records we use, upserted incrementally by write_date (sync_odoo.py).
CREATE TABLE IF NOT EXISTS odoo_records (
    model       text        NOT NULL,
    id          integer     NOT NULL,
    write_date  timestamp   NOT NULL,
    data        jsonb       NOT NULL,
    synced_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model, id)
);
CREATE TABLE IF NOT EXISTS sync_state (
    source       text PRIMARY KEY,           -- odoo:<model> | share
    watermark    text,                       -- last write_date seen (Odoo) or run time (share)
    last_run     timestamptz,
    seen_last    integer,
    changed_last integer
);

-- The share, as read with the read-only account (sync_share.py).
CREATE TABLE IF NOT EXISTS share_quotes (
    path text PRIMARY KEY, customer text, drawing text, material_text text, material_code text, process text,
    qty numeric, tier text, quoted_unit_price numeric, note text, odoo_quote text, excel_only boolean NOT NULL
);
CREATE TABLE IF NOT EXISTS ramesh_notes (n integer PRIMARY KEY, text text NOT NULL);
-- Where the share's price list disagrees with Odoo. Odoo wins; the conflict is logged (W5).
CREATE TABLE IF NOT EXISTS price_conflicts (
    material_code text PRIMARY KEY, share_price numeric, odoo_price numeric, diff_pct numeric,
    winner text NOT NULL DEFAULT 'odoo', logged_at timestamptz NOT NULL DEFAULT now()
);

-- Data-quality findings (normalize.py). Reported to Apex in docs/fde/DATA_QUALITY.md, never written to Odoo.
CREATE TABLE IF NOT EXISTS material_aliases (
    raw text PRIMARY KEY, canonical text, method text NOT NULL, score real, reviewed boolean NOT NULL
);
CREATE TABLE IF NOT EXISTS duplicate_customers (
    ref text PRIMARY KEY, name text, duplicate_of text, duplicate_name text, similarity real, reason text
);

-- One row per part Apex has quoted (Odoo products + Excel-only quotes): what retrieval searches (search.py).
CREATE TABLE IF NOT EXISTS parts (
    code            text PRIMARY KEY,
    name            text,
    family          text,
    drawing         text,
    customer_ref    text,
    material_code   text,
    material_text   text,
    material_source text,          -- bom | alias | fuzzy | unknown
    routing_ops     integer,
    routing_source  text,          -- odoo | estimated:<analog code> | none
    quotes          integer,
    won             integer,
    last_price      numeric,
    won_prices      numeric[],
    lost_prices     numeric[],
    actual_min_per_unit numeric,   -- from finished manufacturing orders (real work-order minutes)
    source          text NOT NULL, -- odoo | excel_only
    search_text     text NOT NULL,
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', search_text)) STORED,
    embedding       vector(384)
);
CREATE INDEX IF NOT EXISTS parts_tsv ON parts USING gin (tsv);

-- S5: the public commodity index behind the price-protection clause (World Bank Pink Sheet, monthly)
CREATE TABLE IF NOT EXISTS commodity_index (
    series      text NOT NULL,
    month       date NOT NULL,           -- first day of the month
    value       double precision NOT NULL,
    unit        text NOT NULL,
    source_url  text NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (series, month)
);

-- S5: what Apex's own drawings of its history parts say (read with the S4 readers from Odoo's attachments)
CREATE TABLE IF NOT EXISTS part_specs (
    code        text PRIMARY KEY,
    family      text,
    material    text,
    fields      jsonb NOT NULL,          -- S4 facts: value + source + confidence
    envelope    double precision[],
    step_volume double precision,        -- from the STEP model, when Odoo has one
    read_at     timestamptz NOT NULL DEFAULT now()
);
-- S12: the archived files' sha256 (repeat detection by file hash)
ALTER TABLE part_specs ADD COLUMN IF NOT EXISTS files jsonb;
