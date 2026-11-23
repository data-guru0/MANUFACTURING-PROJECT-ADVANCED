# S9: OPA's decision log (stdout) keeps every decision with its decision_id (referenced from our audit log), but
# never an approval token or the HTTP headers Envoy forwards (cookies, bearer tokens).
package system.log

import rego.v1

mask contains "/input/token"

mask contains "/input/attributes/request/http/headers"

mask contains "/input/attributes/metadataContext"
