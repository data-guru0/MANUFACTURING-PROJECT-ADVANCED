package apex.data_test

import rego.v1

import data.apex.data as dg

local := {"data_governance": {"keep_export_controlled_local": true}}

remote := {"data_governance": {"keep_export_controlled_local": false}}

test_ordinary_rfqs_may_use_the_llm if dg.external_llm_allowed with input as {"export_controlled": false} with data.policy as local

test_export_controlled_stays_local_when_policy_says_so if {
	not dg.external_llm_allowed with input as {"export_controlled": true} with data.policy as local
}

test_export_controlled_may_go_out_when_policy_allows if {
	dg.external_llm_allowed with input as {"export_controlled": true} with data.policy as remote
}

test_no_policy_means_no if not dg.external_llm_allowed with input as {"export_controlled": true} with data.policy as {}
