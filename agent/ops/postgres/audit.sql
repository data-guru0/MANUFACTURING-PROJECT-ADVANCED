-- S9: the audit log. Append-only and hash-chained: hash = sha256(prev_hash || payload(row)).
-- Applied by agent/ops/provision_access.py as the superuser, so the table is NOT owned by the app's role `apex`.
-- Writers use the role `apex_audit`: INSERT only (no SELECT, UPDATE, DELETE). The chain is computed HERE, in a
-- trigger, under a lock, so a writer can't forge `prev_hash`/`hash` and concurrent writers can't fork the chain.
-- ponytail: a superuser can still rewrite the whole chain consistently; anchor the head hash outside the box (S19).

CREATE TABLE IF NOT EXISTS audit_log (
    id         bigint PRIMARY KEY,
    at         timestamptz NOT NULL,
    kind       text NOT NULL,       -- llm_call | tool_call | opa_decision | human_decision | action | admin
    actor      text NOT NULL,       -- an agent, a service or a person
    email_id   bigint,              -- the job, when there is one
    data       jsonb NOT NULL,      -- what happened (redacted; never a secret or a token)
    prev_hash  text NOT NULL,
    hash       text NOT NULL
);
CREATE SEQUENCE IF NOT EXISTS audit_log_seq;
CREATE INDEX IF NOT EXISTS audit_log_email ON audit_log (email_id, id);

-- The exact text that is hashed. verify_audit_chain recomputes it from the stored row.
CREATE OR REPLACE FUNCTION audit_payload(r audit_log) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT r.id || '|' || to_char(r.at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '|' || r.kind || '|' ||
           r.actor || '|' || coalesce(r.email_id::text, '') || '|' || r.data::text
$$;

CREATE OR REPLACE FUNCTION audit_chain() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE prev text;
BEGIN
    PERFORM pg_advisory_xact_lock(918273645);          -- one writer at a time: ids, times and links stay in order
    SELECT hash INTO prev FROM audit_log ORDER BY id DESC LIMIT 1;
    NEW.id := nextval('audit_log_seq');
    NEW.at := clock_timestamp();
    NEW.prev_hash := coalesce(prev, repeat('0', 64));
    NEW.hash := encode(sha256(convert_to(NEW.prev_hash || audit_payload(NEW), 'UTF8')), 'hex');
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END $$;

DROP TRIGGER IF EXISTS audit_chain ON audit_log;
CREATE TRIGGER audit_chain BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_chain();
DROP TRIGGER IF EXISTS audit_immutable ON audit_log;
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_immutable();
DROP TRIGGER IF EXISTS audit_no_truncate ON audit_log;
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_log FOR EACH STATEMENT EXECUTE FUNCTION audit_immutable();
REVOKE ALL ON audit_log FROM PUBLIC;
