package system.authz_test

import rego.v1

import data.system.authz

test_a_service_may_ask_a_question if authz.allow with input as {"method": "POST", "path": ["v1", "data", "apex", "review", "decision"]}

test_nobody_may_rewrite_data_or_the_kill_switch if {
	not authz.allow with input as {"method": "PUT", "path": ["v1", "data", "policy", "ai_enabled"]}
	not authz.allow with input as {"method": "PATCH", "path": ["v1", "data", "policy"]}
	not authz.allow with input as {"method": "DELETE", "path": ["v1", "data", "policy"]}
}

test_nobody_may_upload_a_policy if not authz.allow with input as {"method": "PUT", "path": ["v1", "policies", "evil"]}

test_nobody_may_read_the_policies_source if not authz.allow with input as {"method": "GET", "path": ["v1", "policies"]}

test_health_and_metrics_stay_open if {
	authz.allow with input as {"method": "GET", "path": ["health"]}
	authz.allow with input as {"method": "GET", "path": ["metrics"]}
}
