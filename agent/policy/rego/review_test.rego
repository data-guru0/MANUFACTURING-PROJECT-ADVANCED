package apex.review_test

import rego.v1

import data.apex.review

small := {"action": "approve", "value_usd": 738.4, "needs_senior": false, "senior": []}

large := {"action": "approve", "value_usd": 16781, "needs_senior": true, "senior": ["quote value $16781 is above $5000"]}

test_an_estimator_approves_a_small_quote if {
	review.decision.allow with input as object.union(small, {"roles": ["estimator"]})
}

test_an_estimator_is_blocked_on_a_large_quote if {
	d := review.decision with input as object.union(large, {"roles": ["estimator"]})
	not d.allow
	d.deny == ["a senior estimator must approve this quote (quote value $16781 is above $5000)"]
}

test_an_estimator_may_still_clarify_or_reject_a_large_quote if {
	review.decision.allow with input as object.union(large, {"roles": ["estimator"], "action": "reject"})
}

test_a_senior_approves_a_large_quote if {
	review.decision.allow with input as object.union(large, {"roles": ["senior_estimator"]})
}

test_an_admin_never_decides_even_as_a_senior if {
	not review.decision.allow with input as object.union(small, {"roles": ["admin", "senior_estimator"]})
}

test_planners_and_auditors_never_decide if {
	not review.decision.allow with input as object.union(small, {"roles": ["planner", "auditor"]})
}

test_no_roles_means_no if {
	not review.decision.allow with input as object.union(small, {"roles": []})
}
