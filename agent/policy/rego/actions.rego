# May this side effect happen? (S8) Asked by the write tools themselves (erp-mcp-write, mail-mcp), not by the worker,
# with the approval token the review screen signed and hashes of what the tool is about to do. Fails closed.
#   - the token must verify (HS256, the screen's key, not expired) and be for this job;
#   - the approver's roles must still allow the decision (the same rules as apex.review);
#   - an email must be exactly the approved one (recipient, subject, body); a quotation must carry the approved prices.
package apex.actions

import rego.v1

import data.apex.review

# Only the review screen and OPA know this key (from .env); the worker never sees it. A FUNCTION, not a rule: a rule's
# value could be read by anyone on the app network with GET /v1/data/apex/actions/key; a function can't be.
signing_key(_) := opa.runtime().env.APEX_APPROVAL_KEY

claims := c if {
	[valid, _, c] := io.jwt.decode_verify(input.token, {"secret": signing_key(0), "alg": "HS256", "iss": "apex-review-ui"})
	valid
}

tools := {"mail.send_reply", "erp.create_draft_quotation", "erp.mark_quote_sent", "mail.send_supplier_rfq"}

sends := {"approve", "edit", "clarify"} # clarify sends questions, not a price

commits := {"approve", "edit"}

approver_may if review.decision.allow with input as {
	"action": claims.action, "roles": claims.roles,
	"needs_senior": claims.needs_senior, "senior": claims.senior,
}

deny contains sprintf("unknown action tool %v", [input.tool]) if not input.tool in tools

deny contains "no valid approval token (signed by the review screen, not expired)" if not claims

deny contains "the approval is for another job" if claims.email_id != input.email_id

deny contains "the approver's roles don't allow this decision" if {
	claims
	not approver_may
}

deny contains sprintf("a %v decision doesn't send anything", [claims.action]) if {
	input.tool == "mail.send_reply"
	not claims.action in sends
}

deny contains "the email differs from the one the person approved" if {
	input.tool == "mail.send_reply"
	input.text_sha256 != claims.text_sha256
}

deny contains sprintf("a %v decision doesn't create a quotation", [claims.action]) if {
	input.tool in {"erp.create_draft_quotation", "erp.mark_quote_sent"}
	not claims.action in commits
}

deny contains "the prices differ from the ones the person approved" if {
	input.tool == "erp.create_draft_quotation"
	input.prices_sha256 != claims.prices_sha256
}

# S17: a supplier RFQ is its own decision; the screen signed it only after apex.suppliers allowed it, and says so
deny contains "a supplier RFQ needs a supplier_rfq approval" if {
	input.tool == "mail.send_supplier_rfq"
	claims.action != "supplier_rfq"
}

deny contains "the supplier RFQ differs from the one the person approved" if {
	input.tool == "mail.send_supplier_rfq"
	input.text_sha256 != claims.text_sha256
}

deny contains "nothing goes to suppliers for an NDA-restricted customer or an export-controlled job" if {
	input.tool == "mail.send_supplier_rfq"
	not claims.supplier_ok == true
}

decision := {"allow": count(deny) == 0, "deny": sort(deny)}
