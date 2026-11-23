"""The audit log (S9): one append-only, hash-chained table (agent/ops/postgres/audit.sql) that every service writes to
through the INSERT-only role `apex_audit` (APEX_AUDIT_DB_URL). The database computes the chain; we only add rows.

What is recorded: LLM calls (agent, alias, cost, call id, trace id, a Presidio-redacted preview), MCP tool calls,
OPA decisions (with OPA's decision_id, so the full decision log entry can be found), human decisions and every
outbound action. Never a secret or a token: `record` drops keys that look like one.

Fails CLOSED: if the audit row can't be written, the caller gets an error and the step doesn't count as done.
"""

import json
import os
import threading
from typing import Any

import psycopg

SECRET_KEYS = {"token", "approval", "authorization", "password", "secret", "api_key", "key"}
_conn: psycopg.Connection[Any] | None = None
_lock = threading.Lock()  # the graph's fan-out runs nodes in threads; one connection, one writer at a time


class AuditUnavailable(RuntimeError):
    """The audit row couldn't be written: the action must not be treated as done."""


def clean(v: Any) -> Any:
    """Drop anything secret-looking; keep the rest JSON-able and short."""
    if isinstance(v, dict):
        return {k: ("[removed]" if k.lower() in SECRET_KEYS else f"[{len(x)} chars of base64]" if k == "data_b64" and isinstance(x, str)
                    else clean(x)) for k, x in v.items()}  # fmt: skip
    if isinstance(v, list | tuple):
        return [clean(x) for x in v][:50]
    if isinstance(v, str):
        return v[:2000]
    return v


def record(kind: str, actor: str, data: dict[str, Any], email_id: int | None = None) -> None:
    global _conn
    url = os.environ.get("APEX_AUDIT_DB_URL")
    if not url:
        raise AuditUnavailable("APEX_AUDIT_DB_URL is not set: this process can't write the audit log")
    payload = json.dumps(clean(data), ensure_ascii=False, default=str)
    with _lock:
        for attempt in (1, 2):  # one reconnect (Postgres restarted, idle connection dropped)
            try:
                if _conn is None or _conn.closed:
                    _conn = psycopg.connect(url, autocommit=True)
                _conn.execute("INSERT INTO audit_log (kind, actor, email_id, data) VALUES (%s, %s, %s, %s::jsonb)",
                              (kind, actor, email_id, payload))  # fmt: skip
                return
            except psycopg.OperationalError as e:
                _conn = None
                if attempt == 2:
                    raise AuditUnavailable(f"audit log unreachable: {e}") from e
