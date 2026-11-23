# Why a costed RFQ needs more than a routine approval (S9). Each reason has a LEVEL, set in policy.yaml -> escalation
# (Meera decides, not the code):
#   escalate  the job goes to a person before any draft is trusted (risk route "escalate")
#   senior    only a senior estimator may approve it (apex.review)
#   check     shown to the reviewer as something to verify; doesn't change who may approve
package apex.escalation

import rego.v1

cfg := data.policy.escalation

limit := data.policy.graph.senior_above_usd

reasons contains {"reason": "the security gate raised a red banner (possible injection, spoofing or a bad file)", "level": "escalate"} if {
	input.red_banner
}

reasons contains {"reason": "export control", "level": "senior"} if input.extraction_route == "senior"

reasons contains {"reason": sprintf("quote value $%v is above $%v", [round(input.value_usd), round(limit)]), "level": "senior"} if {
	input.value_usd > limit
}

reasons contains {"reason": sprintf("margin %v %% is below the floor of %v %%", [input.margin_pct, cfg.margin_floor_pct]), "level": cfg.levels.margin_below_floor} if {
	is_number(input.margin_pct)
	input.margin_pct < cfg.margin_floor_pct
}

# S14: and a ceiling. The margin curve is advisory; a price far above policy (e.g. a reviewer chasing expected profit
# on a curve that isn't validated) needs a senior too.
reasons contains {"reason": sprintf("margin %v %% is above the ceiling of %v %%", [input.margin_max_pct, cfg.margin_ceiling_pct]), "level": cfg.levels.margin_above_ceiling} if {
	is_number(input.margin_max_pct)
	input.margin_max_pct > cfg.margin_ceiling_pct
}

reasons contains {"reason": "new customer (not in Odoo yet)", "level": cfg.levels.new_customer} if input.new_customer

reasons contains {"reason": sprintf("%v value(s) read with low confidence", [input.low_confidence]), "level": cfg.levels.low_confidence} if {
	input.low_confidence > 0
}

reasons contains {"reason": "the customer's requested date can't be met", "level": cfg.levels.infeasible_date} if input.infeasible_date

at(level) := sort([r.reason | some r in reasons; r.level == level])
