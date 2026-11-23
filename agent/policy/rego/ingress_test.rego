package apex.ingress_test

import rego.v1

import data.apex.ingress

req(method, path, roles, azp) := {"attributes": {
	"request": {"http": {"method": method, "path": path}},
	"metadataContext": {"filterMetadata": {"envoy.filters.http.jwt_authn": {"jwt_payload": {
		"azp": azp,
		"realm_access": {"roles": roles},
	}}}},
}}

test_every_staff_role_opens_the_queue if {
	every r in ["estimator", "senior_estimator", "planner", "auditor", "admin"] {
		ingress.allow with input as req("GET", "/", [r], "review-ui")
	}
}

test_an_estimator_may_send_a_decision if {
	ingress.allow with input as req("POST", "/jobs/12/decide", ["estimator"], "review-ui")
}

test_planner_auditor_and_admin_may_not_send_a_decision if {
	every r in ["planner", "auditor", "admin"] {
		not ingress.allow with input as req("POST", "/jobs/12/decide", [r], "review-ui")
	}
}

test_no_other_post_route if {
	not ingress.allow with input as req("POST", "/jobs/12", ["senior_estimator"], "review-ui")
}

test_a_token_for_another_client_is_refused if {
	not ingress.allow with input as req("GET", "/", ["estimator"], "account-console")
}

test_no_verified_token_is_refused if {
	not ingress.allow with input as {"attributes": {"request": {"http": {"method": "GET", "path": "/"}}}}
}

test_an_estimator_may_ask_what_if_and_apply_an_option if {
	ingress.allow with input as req("POST", "/jobs/53/whatif", ["estimator"], "review-ui")
	ingress.allow with input as req("POST", "/jobs/53/whatif/apply", ["senior_estimator"], "review-ui")
}

test_an_auditor_may_not_spend_the_assistant_budget if {
	not ingress.allow with input as req("POST", "/jobs/53/whatif", ["auditor"], "review-ui")
	not ingress.allow with input as req("POST", "/jobs/53/whatif/other", ["estimator"], "review-ui")
}
