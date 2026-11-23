"""share-mcp: read-only tools over \\\\fileserver\\Quotes as `agent-ro` (S2). Samba itself refuses writes for
this account, so even a bug here can't change the share."""

import io
import logging
from typing import Any

import openpyxl
import smbclient
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from apex_agent.mcp_servers.common import guard, serve
from apex_agent.settings import ShareSettings, load_as

mcp: MCPServer[Any] = MCPServer("share-mcp", instructions="Read Apex's file share: old quotes, the calculator, Ramesh's notes.")
logging.getLogger("smbprotocol").setLevel(logging.ERROR)  # its INFO log is one line per SMB packet
CALCULATOR = "Pricing_Calculator_v7_FINAL(2).xlsx"
# The calculator's input cells (legacy L4 layout): label -> cell
QUOTE_CELLS = {"customer": "B2", "drawing": "B3", "date": "B4", "material": "B5", "process": "B6", "qty": "B19",
               "tier": "B20", "quoted_unit_price": "B40", "note": "B41", "odoo_quote": "B42"}  # fmt: skip


def root() -> str:
    s = load_as(ShareSettings)
    smbclient.register_session(s.server, username=s.user, password=s.password.get_secret_value(), port=s.port)
    return rf"\\{s.server}\{s.name}"


def read_book(rel: str) -> openpyxl.Workbook:
    if ".." in rel or rel.startswith(("\\", "/")):
        raise ToolError("path must be relative to the share root")
    with smbclient.open_file(root() + "\\" + rel.replace("/", "\\"), mode="rb", port=load_as(ShareSettings).port) as f:
        return openpyxl.load_workbook(io.BytesIO(f.read()), data_only=False)


def list_quote_files(customer: str | None = None) -> list[str]:
    base, port = root() + r"\Customers", load_as(ShareSettings).port
    out = []
    for c in sorted(smbclient.listdir(base, port=port)):
        if customer and customer.lower() not in c.lower():
            continue
        out += [f"Customers\\{c}\\{f}" for f in sorted(smbclient.listdir(base + "\\" + c, port=port)) if f.endswith(".xlsx")]
    return out


def quote_fields(rel: str) -> dict[str, Any]:
    ws = read_book(rel)["Calculator"]
    out: dict[str, Any] = {"path": rel}
    for key, cell in QUOTE_CELLS.items():
        v = ws[cell].value
        out[key] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


@mcp.tool()
def list_quotes(ctx: Context[Any, Any], customer: str | None = None) -> list[str]:
    """Quote spreadsheets under Customers\\, optionally only folders whose name contains `customer`."""
    guard(ctx, "share.list_quotes")
    return list_quote_files(customer)


@mcp.tool()
def read_quote(path: str, ctx: Context[Any, Any]) -> dict[str, Any]:
    """The key cells of one quote spreadsheet (customer, drawing, material, qty, quoted price, note, Odoo number)."""
    guard(ctx, "share.read_quote")
    return quote_fields(path)


@mcp.tool()
def ramesh_notes(ctx: Context[Any, Any]) -> list[str]:
    """The free-text rules on the calculator's "Ramesh notes" sheet (W6)."""
    guard(ctx, "share.ramesh_notes")
    return [str(r[1].value) for r in read_book(CALCULATOR)["Ramesh notes"].iter_rows(min_row=2) if r[1].value]


@mcp.tool()
def material_prices(ctx: Context[Any, Any]) -> list[dict[str, Any]]:
    """The calculator's material price table ($/kg) as it is on the share (may be stale: W5)."""
    guard(ctx, "share.material_prices")
    rows = read_book(CALCULATOR)["Materials"].iter_rows(min_row=2)
    return [{"name": r[0].value, "code": r[1].value, "price_per_kg": r[3].value} for r in rows if r[1].value]


def main() -> None:
    serve(mcp, "share-mcp")


if __name__ == "__main__":
    main()
