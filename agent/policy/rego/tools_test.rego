package apex.tools_test

import data.apex.tools
import rego.v1

policy := {"tools": {"retrieve": ["erp.get_product"], "act": ["erp.create_draft_quotation"]}}

test_listed_tool_is_allowed if tools.allow with input as {"node": "retrieve", "tool": "erp.get_product"} with data.policy as policy

test_write_tool_denied_outside_act if not tools.allow with input as {"node": "retrieve", "tool": "erp.create_draft_quotation"} with data.policy as policy

test_write_tool_allowed_for_act if tools.allow with input as {"node": "act", "tool": "erp.create_draft_quotation"} with data.policy as policy

test_unknown_node_denied if not tools.allow with input as {"node": "draft_reply", "tool": "erp.get_product"} with data.policy as policy

test_missing_input_denied if not tools.allow with input as {} with data.policy as policy
