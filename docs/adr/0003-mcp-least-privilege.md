# ADR 0003: MCP tool servers for least-privilege access to legacy systems

- **Status:** accepted (S0, 2026-11-20)
- **Deciders:** FDE team; reviewed with Vikram (IT) and Meera (sponsor)

## Context
Today everyone uses the shared `office` login (W3). If the agent got Odoo's admin password, one prompt injection could change anything.

## Decision
The agent reaches Odoo and the share **only through MCP tool servers** (`erp-mcp`, `share-mcp`, `docs-mcp`). Each server holds its own scoped credential (`erp_reader` read-only, `erp_integration` with sales/purchase rights, a read-only share user). Read and write tools are separate sets. Every tool call asks OPA (`tools.rego`) whether *this node* may call *this tool*.

## Consequences
+ The blast radius of a compromised prompt is one tool's permissions, and OPA can refuse it.
+ Tools are reusable by other agents and inspectable.
− An extra process per legacy system, and tool schemas to maintain.

## Addendum (S2, as built)
- Three servers: `erp-mcp-read` (as `erp_reader`), `erp-mcp-write` (as `erp_integration`, only the `act` node may call it) and `share-mcp` (as `agent-ro`, which Samba itself keeps read-only). Every tool asks OPA `apex/tools/allow` with the calling node from the `X-Apex-Node` header; the header is trusted inside the internal network until S9 binds it to a signed token.
- The **batch knowledge sync** (a scheduled job, not a graph node) reads Odoo and the share **directly**, with the same read-only accounts, because copying ~1,100 records through one-call-per-record tools would be slow and pointless. It cannot write: the accounts don't allow it.
- The mailbox: Dovecot **master user** `agent` logs in as `rfq@apex.local*agent` with its own password, and Dovecot logs every such login. A master user could open any mailbox; our code and OPA restrict it to rfq@ (upgrade: an ACL shared folder, recommended in S9).
