# May Apex ask suppliers about this job at all? (S17, W18). Asked when the drafts are made (the worker) and again
# before the review screen signs a send. Fails closed: no answer = no.
package apex.suppliers

import rego.v1

default send_allowed := false

send_allowed if count(deny) == 0

deny contains "the customer is NDA-restricted: their parts and drawings never leave Apex" if input.nda_restricted == true

deny contains sprintf("export-controlled job (%v): no third party may see it", [input.export_control]) if {
	# as extraction.rego (S4): the extractor gives a string ("ITAR ...") or a boolean; null, false and "" mean no
	input.export_control != null
	input.export_control != false
	input.export_control != ""
}

decision := {"allow": send_allowed, "deny": sort(deny)}
