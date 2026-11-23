"""Shared plumbing for the MCP tool servers (ADR 0003): every tool asks OPA first, then works with this
server's own scoped credential. The caller says which graph node it is in the `X-Apex-Node` header.
ponytail: the header is trusted inside the internal `app` network; S9 binds it to a signed token (mTLS/JWT)."""

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from apex_agent import opa
from apex_agent.settings import McpSettings, load_as


def guard(ctx: Context[Any, Any], tool: str) -> None:
    """Raise unless OPA (apex/tools/allow) lets the calling node use `tool`. Fails closed."""
    headers = ctx.headers or {}
    node, agent = headers.get("x-apex-node", ""), headers.get("x-apex-agent", "")
    if not opa.allowed(load_as(McpSettings).opa_url, "apex/tools/allow", {"node": node, "agent": agent, "tool": tool}):
        raise ToolError(f"OPA denied tool {tool!r} for node {node!r}")


def require_approval(tool: str, email_id: int, token: str, **hashes: str) -> None:
    """S8: a side effect needs a person's signed approval of exactly this content (OPA apex/actions). Fails closed."""
    d = opa.decide(
        load_as(McpSettings).opa_url, "apex/actions/decision", {"tool": tool, "email_id": email_id, "token": token, **hashes}
    )
    if not isinstance(d, dict) or d.get("allow") is not True:
        why = "; ".join(d.get("deny") or ["not allowed"]) if isinstance(d, dict) else "policy service (OPA) unavailable"
        raise ToolError(f"OPA refused {tool}: {why}")


def serve(server: MCPServer[Any], service: str) -> None:
    """Streamable HTTP on :port, accepting only Host headers for this service (DNS-rebinding protection)."""
    s = load_as(McpSettings)
    server.run("streamable-http", host="0.0.0.0", port=s.port, stateless_http=True, json_response=True,  # nosec B104 (inside its container, on the internal network only)
               transport_security=TransportSecuritySettings(allowed_hosts=[f"{service}:*", service]))  # fmt: skip
