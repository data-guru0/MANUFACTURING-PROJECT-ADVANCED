# The security gate's decisions (S3). Facts are measured in Python; OPA decides what they mean.
# Callers fail closed: no answer from OPA = quarantine the file / red banner + human.
package apex.gate

import rego.v1

cfg := data.policy.gate

# ---- one file: why it may NOT go on (empty set = clean)
file_reasons contains sprintf("virus found: %s", [input.virus]) if input.virus != null

file_reasons contains sprintf("file type not allowed: %s", [input.true_type]) if {
	input.virus == null
	not cfg.files.allowed_types[input.true_type]
}

file_reasons contains sprintf("the name says .%s but the file is really %s", [input.ext, input.true_type]) if {
	exts := cfg.files.allowed_types[input.true_type]
	not input.ext in exts
}

file_reasons contains sprintf("ZIP: %s", [p]) if some p in input.zip_problems

# S9 (red team): a huge file is never parsed or sent anywhere
file_reasons contains sprintf("file is %v MB, above the %v MB limit", [input.size_mb, cfg.files.max_file_mb]) if {
	input.size_mb > cfg.files.max_file_mb
}

file_reasons contains "a ZIP inside a ZIP" if {
	input.true_type == "application/zip"
	input.depth >= cfg.files.zip.max_depth
}

# ---- the whole email: red banner and a human, with the reasons shown to the reviewer
reasons contains sprintf("prompt injection suspected (score %.2f, in %s)", [input.injection.score, input.injection.where]) if {
	input.injection.score >= cfg.injection_threshold
}

reasons contains sprintf("attachment quarantined: %s (%s)", [f.filename, concat("; ", f.reasons)]) if {
	some f in input.files
	f.verdict == "quarantined"
}

reasons contains sprintf("display name claims %s, but the address is not theirs", [input.sender.display_name_claims]) if {
	input.sender.spoof == true
}

reasons contains "sender could not be checked against Odoo" if input.sender.spoof == null

reasons contains sprintf("DKIM/DMARC failed for %s", [input.sender.from_domain]) if input.sender.dmarc == "fail"

decision := {"red_banner": count(reasons) > 0, "needs_human": count(reasons) > 0, "reasons": sort(reasons)}
