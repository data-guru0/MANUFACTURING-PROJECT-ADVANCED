"""Minimal Odoo JSON-RPC client (stdlib only). Reused by every legacy seed script."""

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ODOO_URL = os.environ.get("ODOO_URL", "http://127.0.0.1:8069")
ODOO_DB = "apex"


def env(key: str) -> str:
    """Read a value from the environment, else from .env. Never log the result."""
    if key in os.environ:
        return os.environ[key]
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    raise KeyError(f"{key} missing in .env (run: make secrets)")


class OdooError(RuntimeError):
    pass


def jsonrpc(service: str, method: str, *args: Any, url: str = ODOO_URL, timeout: float = 120) -> Any:
    body = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"service": service, "method": method, "args": args},
    }
    req = urllib.request.Request(f"{url}/jsonrpc", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if "error" in data:
        err = data["error"]
        raise OdooError(f"{service}.{method}: {err.get('data', {}).get('message') or err.get('message')}")
    return data.get("result")  # methods returning None omit "result"


class Odoo:
    """A logged-in session. `call(model, method, *args, **kwargs)` = execute_kw."""

    def __init__(self, login: str, password: str, db: str = ODOO_DB, url: str = ODOO_URL) -> None:
        self.db, self.url, self.password = db, url, password
        uid = jsonrpc("common", "authenticate", db, login, password, {}, url=url)
        if not uid:
            raise OdooError(f"login failed for {login!r}")
        self.uid: int = uid

    def call(self, model: str, method: str, *args: Any, timeout: float = 120, **kwargs: Any) -> Any:
        return jsonrpc(
            "object",
            "execute_kw",
            self.db,
            self.uid,
            self.password,
            model,
            method,
            list(args),
            kwargs,
            url=self.url,
            timeout=timeout,
        )

    def ensure(self, model: str, domain: list[Any], vals: dict[str, Any]) -> int:
        """Idempotent upsert: update the first match, else create. Returns the id."""
        ids: list[int] = self.call(model, "search", domain, limit=1)
        if ids:
            self.call(model, "write", ids, vals)
            return ids[0]
        new_id: int = self.call(model, "create", vals)
        return new_id

    def xmlid(self, xml_id: str) -> int:
        module, name = xml_id.split(".", 1)
        rows = self.call(
            "ir.model.data",
            "search_read",
            [("module", "=", module), ("name", "=", name)],
            fields=["res_id"],
            limit=1,
        )
        if not rows:
            raise OdooError(f"unknown xml id {xml_id}")
        res_id: int = rows[0]["res_id"]
        return res_id
