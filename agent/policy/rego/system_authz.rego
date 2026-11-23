# S9: who may use OPA's own REST API (OPA runs with --authorization=basic). Found in S9: OPA's API was open to every
# service on the app network, including PUT /v1/data and /v1/policies, so a compromised worker could have rewritten
# the rules (or turned the kill switch off). Now services may only ASK. Policy and data come only from the files
# (agent/policy/rego, agent/config/policy.yaml) at start: `make policy`.
package system.authz

import rego.v1

default allow := false

# ask a question: POST /v1/data/<rule path> (every service's client uses POST)
allow if {
	input.method == "POST"
	input.path[0] == "v1"
	input.path[1] == "data"
}

# liveness and Prometheus
allow if {
	input.method == "GET"
	input.path in [["health"], ["metrics"]]
}
