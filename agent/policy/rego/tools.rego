# Which node may call which MCP tool (S2). Every MCP tool asks this before doing anything; fails closed.
package apex.tools

import rego.v1

default allow := false

allow if input.tool in data.policy.tools[input.node]
