"""Minimal Odoo JSON-RPC client for the agent (separate from the legacy seed scripts' client on purpose:
the agent is a different system and logs in only with its own scoped accounts)."""

from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from apex_agent.settings import OdooSettings


class OdooError(RuntimeError):
    pass


class OdooUnavailable(ToolError):
    """S19: Odoo can't be reached. A ToolError, so an MCP tool passes the words through (a generic error would hide them)
    and the worker knows to WAIT for Odoo instead of failing the job."""


class Odoo:
    def __init__(self, s: OdooSettings) -> None:
        self.s, self.http = s, httpx.Client(base_url=s.url, timeout=120)
        uid = self._rpc("common", "authenticate", s.db, s.login, s.password.get_secret_value(), {})
        if not uid:
            raise OdooError(f"Odoo login failed for {s.login!r}")
        self.uid: int = uid

    def _rpc(self, service: str, method: str, *args: Any) -> Any:
        try:
            r = self.http.post("/jsonrpc", json={"jsonrpc": "2.0", "method": "call",
                                                 "params": {"service": service, "method": method, "args": args}})  # fmt: skip
        except httpx.TransportError as e:
            raise OdooUnavailable(f"Odoo unreachable: {type(e).__name__}") from e
        if r.status_code >= 502:
            raise OdooUnavailable(f"Odoo unreachable: HTTP {r.status_code}")
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            err = data["error"]
            raise OdooError((err.get("data") or {}).get("message") or err.get("message"))
        return data.get("result")

    def call(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        return self._rpc("object", "execute_kw", self.s.db, self.uid, self.s.password.get_secret_value(), model, method,
                         list(args), kwargs)  # fmt: skip
