# Who may decide a quote at human review (S7). Asked by the review screen before it accepts a decision, and again
# by QuoteGraph.resume before the graph moves on (every caller). Callers treat "no answer" as "no" (fail closed).
# The approval limit (graph.senior_above_usd) and export control come from risk.rego's `needs_senior`.
package apex.review

import rego.v1

deciders := {"estimator", "senior_estimator"}

actions := {"approve", "edit", "recost", "clarify", "reject", "supplier_rfq"} # supplier_rfq: S17, commits nothing

# approve and edit commit Apex to a price and a date; the others don't
commits := {"approve", "edit"}

roles := {r | some r in input.roles}

deny contains "admins run the system and never decide quotes" if "admin" in roles

deny contains "only an estimator or a senior estimator decides quotes" if count(roles & deciders) == 0

deny contains sprintf("unknown action %v", [input.action]) if not input.action in actions

deny contains sprintf("a senior estimator must approve this quote (%s)", [concat("; ", input.senior)]) if {
	input.action in commits
	input.needs_senior
	not "senior_estimator" in roles
}

decision := {"allow": count(deny) == 0, "deny": sort(deny)}
