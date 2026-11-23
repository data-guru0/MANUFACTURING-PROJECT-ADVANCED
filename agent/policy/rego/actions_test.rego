package apex.actions_test

import rego.v1

import data.apex.actions

k := "test-key-not-a-secret"

mock_key(_) := k

token(c) := io.jwt.encode_sign({"typ": "JWT", "alg": "HS256"}, object.union({
	"iss": "apex-review-ui", "email_id": 7, "action": "approve", "roles": ["estimator"],
	"needs_senior": false, "senior": [], "text_sha256": "T", "prices_sha256": "P",
	"exp": (time.now_ns() / 1000000000) + 3600,
}, c), {"kty": "oct", "k": base64url.encode_no_pad(k)})

ask(tool, c, extra) := d if {
	d := actions.decision with actions.signing_key as mock_key
		with input as object.union({"tool": tool, "email_id": 7, "token": token(c), "text_sha256": "T", "prices_sha256": "P"}, extra)
}

test_the_approved_email_may_be_sent if ask("mail.send_reply", {}, {}).allow

test_the_approved_prices_become_a_quotation if ask("erp.create_draft_quotation", {}, {}).allow

test_a_changed_email_is_refused if {
	d := ask("mail.send_reply", {}, {"text_sha256": "OTHER"})
	d.deny == ["the email differs from the one the person approved"]
}

test_changed_prices_are_refused if {
	not ask("erp.create_draft_quotation", {}, {"prices_sha256": "OTHER"}).allow
}

test_a_token_for_another_job_is_refused if not ask("mail.send_reply", {"email_id": 8}, {}).allow

test_an_expired_token_is_refused if not ask("mail.send_reply", {"exp": 1}, {}).allow

test_a_token_signed_with_another_key_is_refused if {
	forged := io.jwt.encode_sign({"typ": "JWT", "alg": "HS256"}, {"iss": "apex-review-ui", "email_id": 7, "action": "approve",
		"roles": ["senior_estimator"], "needs_senior": false, "senior": [], "text_sha256": "T", "prices_sha256": "P"},
		{"kty": "oct", "k": base64url.encode_no_pad("guessed")})
	d := actions.decision with actions.signing_key as mock_key
		with input as {"tool": "mail.send_reply", "email_id": 7, "token": forged, "text_sha256": "T"}
	not d.allow
}

test_no_token_is_refused if {
	d := actions.decision with actions.signing_key as mock_key with input as {"tool": "mail.send_reply", "email_id": 7, "text_sha256": "T"}
	not d.allow
}

test_an_estimator_token_on_a_senior_quote_is_refused if {
	not ask("erp.create_draft_quotation", {"needs_senior": true, "senior": ["value"]}, {}).allow
}

test_a_clarification_sends_but_never_creates_a_quotation if {
	ask("mail.send_reply", {"action": "clarify"}, {}).allow
	not ask("erp.create_draft_quotation", {"action": "clarify"}, {}).allow
}

test_a_rejection_sends_nothing if not ask("mail.send_reply", {"action": "reject"}, {}).allow
