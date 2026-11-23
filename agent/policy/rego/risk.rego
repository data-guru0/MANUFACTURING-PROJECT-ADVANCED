# Where an RFQ goes after it is costed (S6, node risk_policy). Every route ends in human review: nothing is sent
# without a person. The caller treats "no answer" as "escalate" (fail closed).
# S9: the reasons themselves (value, margin floor, new customer, low confidence, injection, export control, an
# infeasible date) come from apex.escalation, each at the level policy.yaml gives it.
package apex.risk

import rego.v1

import data.apex.escalation

cfg := data.policy.graph

escalate contains r if some r in escalation.at("escalate")

escalate contains sprintf("the LLM budget for this job was exceeded ($%.3f > $%.2f)", [input.spent_usd, cfg.job_budget_usd]) if {
	input.spent_usd > cfg.job_budget_usd
}

escalate contains "nothing could be costed" if {
	count(input.clarification) == 0
	count(input.estimates) == 0
	not input.no_bid
}

senior contains r if some r in escalation.at("senior")

route := "escalate" if count(escalate) > 0

# S11: Apex can't make any of the parts: a polite decline, still approved by a person
route := "no_bid" if {
	count(escalate) == 0
	input.no_bid
}

route := "clarify" if {
	count(escalate) == 0
	not input.no_bid
	count(input.clarification) > 0
}

route := "quote" if {
	count(escalate) == 0
	not input.no_bid
	count(input.clarification) == 0
}

decision := {"route": route, "escalate": sort(escalate), "senior": sort(senior), "needs_senior": count(senior) > 0,
	"check": escalation.at("check")}
