# The review screen's front door (S7): Envoy ext_authz asks this for EVERY request on https://localhost:8448, after
# Envoy has logged the user in with Keycloak (oauth2 filter) and verified the token (jwt_authn). The verified token's
# claims arrive as jwt_authn metadata, never from a header the browser could set. No answer = 403 (Envoy fails closed).
# Coarse on purpose: who may open the screen, who may send a decision. WHICH decision is allowed is apex.review.
package apex.ingress

import rego.v1

default allow := false

claims := input.attributes.metadataContext.filterMetadata["envoy.filters.http.jwt_authn"].jwt_payload

roles := {r | some r in claims.realm_access.roles}

staff := {"estimator", "senior_estimator", "planner", "auditor", "admin"}

deciders := {"estimator", "senior_estimator"}

http := input.attributes.request.http

# a token issued to the review screen, not one minted for some other client of the realm
for_us if claims.azp == "review-ui"

allow if {
	for_us
	http.method == "GET"
	count(roles & staff) > 0
}

allow if {
	for_us
	http.method == "POST"
	regex.match(`^/jobs/[0-9]+/decide$`, http.path)
	count(roles & deciders) > 0
}

# S16: the what-if assistant (it spends an LLM budget) and "apply as option": the people who quote
allow if {
	for_us
	http.method == "POST"
	regex.match(`^/jobs/[0-9]+/(whatif(/apply)?|suppliers/[0-9]+/send)$`, http.path)
	count(roles & deciders) > 0
}
