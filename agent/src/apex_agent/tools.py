"""Call an MCP tool as a given graph node (S2). The graph nodes use `call_tool`; the CLI is for tests and class demos.

Usage: python -m apex_agent.tools <server> <tool> '<json args>' --node retrieve
       servers: erp-mcp-read | erp-mcp-write | share-mcp | mail-mcp
"""

import argparse
import asyncio
import json
import os
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from apex_agent import audit

SERVERS = {"erp-mcp-read": "http://erp-mcp-read:8000/mcp", "erp-mcp-write": "http://erp-mcp-write:8000/mcp",
           "share-mcp": "http://share-mcp:8000/mcp", "mail-mcp": "http://mail-mcp:8000/mcp"}  # fmt: skip


async def call_tool(server: str, tool: str, args: dict[str, Any], node: str) -> tuple[bool, Any]:
    """(ok, result): ok is False when the tool raised (e.g. OPA denied it); never raises for a tool error."""
    headers = {"X-Apex-Node": node, "X-Apex-Agent": os.environ.get("APEX_AGENT", "")}
    async with httpx2.AsyncClient(headers=headers, timeout=120) as http, \
            Client(streamable_http_client(SERVERS[server], http_client=http)) as client:  # fmt: skip
        res = await client.call_tool(tool, args)
    text = "".join(getattr(c, "text", "") for c in res.content)
    email_id = args.get("email_id") if isinstance(args.get("email_id"), int) else None
    audit.record("tool_call", node, {"server": server, "tool": tool, "ok": not res.is_error, "args": args,
                                     "result": text[:500]}, email_id)  # S9  # fmt: skip
    if res.is_error:
        return False, text
    data = res.structured_content
    if isinstance(data, dict) and set(data) == {"result"}:  # MCP wraps a non-object result (e.g. a list) like this
        data = data["result"]
    return True, data if data is not None else text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("server", choices=sorted(SERVERS))
    ap.add_argument("tool")
    ap.add_argument("args", nargs="?", default="{}")
    ap.add_argument("--node", required=True)
    a = ap.parse_args()
    ok, result = asyncio.run(call_tool(a.server, a.tool, json.loads(a.args), a.node))
    print(json.dumps({"ok": ok, "result": result}, default=str))


if __name__ == "__main__":
    main()
