package apex.risk_test

import rego.v1

import data.apex.risk

policy := {"graph": {"job_budget_usd": 0.25, "senior_above_usd": 5000}, "escalation": {"margin_floor_pct": 12.0, "margin_ceiling_pct": 45.0, "levels": {
	"margin_below_floor": "senior", "margin_above_ceiling": "senior", "new_customer": "senior", "low_confidence": "check", "infeasible_date": "check",
}}}

clean := {"red_banner": false, "spent_usd": 0.01, "clarification": [], "estimates": [{"part": "D-1"}],
	"extraction_route": "estimator", "value_usd": 900}

test_a_clean_rfq_is_quoted if {
	risk.decision.route == "quote" with input as clean with data.policy as policy
	not risk.decision.needs_senior with input as clean with data.policy as policy
}

test_missing_information_asks_the_customer if {
	risk.decision.route == "clarify" with input as object.union(clean, {"clarification": ["which material?"]})
		with data.policy as policy
}

test_a_red_banner_or_a_blown_budget_escalates if {
	risk.decision.route == "escalate" with input as object.union(clean, {"red_banner": true}) with data.policy as policy
	risk.decision.route == "escalate" with input as object.union(clean, {"spent_usd": 0.9}) with data.policy as policy
}

test_export_control_or_a_big_quote_needs_a_senior if {
	risk.decision.needs_senior with input as object.union(clean, {"extraction_route": "senior"}) with data.policy as policy
	risk.decision.needs_senior with input as object.union(clean, {"value_usd": 12000}) with data.policy as policy
}

test_the_senior_reason_reads_well_with_an_integer_limit if {
	d := risk.decision with input as {"red_banner": false, "spent_usd": 0, "estimates": ["P"], "clarification": [], "extraction_route": "normal", "value_usd": 231800.4}
		with data.policy as policy
	d.senior == ["quote value $231800 is above $5000"]
}

test_a_low_margin_or_a_new_customer_needs_a_senior if {
	d := risk.decision with input as object.union(clean, {"margin_pct": 9.0, "new_customer": true}) with data.policy as policy
	d.senior == ["margin 9 % is below the floor of 12 %", "new customer (not in Odoo yet)"]
}

test_a_margin_above_the_ceiling_needs_a_senior if {
	d := risk.decision with input as object.union(clean, {"margin_pct": 20.0, "margin_max_pct": 52.0}) with data.policy as policy
	d.senior == ["margin 52 % is above the ceiling of 45 %"]
}

test_a_margin_inside_floor_and_ceiling_is_routine if {
	d := risk.decision with input as object.union(clean, {"margin_pct": 20.0, "margin_max_pct": 30.0}) with data.policy as policy
	not d.needs_senior
}

test_low_confidence_and_an_infeasible_date_are_shown_but_dont_block if {
	d := risk.decision with input as object.union(clean, {"low_confidence": 2, "infeasible_date": true}) with data.policy as policy
	not d.needs_senior
	d.check == ["2 value(s) read with low confidence", "the customer's requested date can't be met"]
	d.route == "quote"
}

test_the_levels_are_policy_data if {
	strict := object.union(policy, {"escalation": {"levels": {"infeasible_date": "senior"}}})
	risk.decision.needs_senior with input as object.union(clean, {"infeasible_date": true}) with data.policy as strict
}

test_parts_apex_cannot_make_get_a_decline_not_an_escalation if {
	d := risk.decision with input as object.union(clean, {"estimates": [], "no_bid": true}) with data.policy as policy
	d.route == "no_bid"
	d.escalate == []
}

test_a_red_banner_still_wins_over_a_no_bid if {
	risk.decision.route == "escalate" with input as object.union(clean, {"estimates": [], "no_bid": true, "red_banner": true})
		with data.policy as policy
}
