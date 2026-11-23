package apex.gate_test

import rego.v1

import data.apex.gate

policy := {"gate": {"injection_threshold": 0.5, "files": {
	"allowed_types": {"application/pdf": ["pdf"], "application/zip": ["zip"]},
	"zip": {"max_depth": 1},
}}}

pdf := {"filename": "D-1.pdf", "ext": "pdf", "true_type": "application/pdf", "virus": null, "zip_problems": [], "depth": 0}

clean_email := {
	"injection": {"score": 0.01, "where": "email body"},
	"files": [{"filename": "D-1.pdf", "verdict": "clean", "reasons": []}],
	"sender": {"from_domain": "customers.local", "dmarc": "pass", "spoof": false},
}

test_a_real_pdf_is_clean if {
	count(gate.file_reasons) == 0 with input as pdf with data.policy as policy
}

test_eicar_is_quarantined if {
	"virus found: Eicar-Test-Signature" in gate.file_reasons with input as object.union(pdf, {"virus": "Eicar-Test-Signature"})
		with data.policy as policy
}

test_a_zip_named_pdf_is_refused if {
	some r in gate.file_reasons with input as object.union(pdf, {"true_type": "application/zip"}) with data.policy as policy
	contains(r, "the name says .pdf")
}

test_an_exe_is_refused if {
	some r in gate.file_reasons with input as object.union(pdf, {"true_type": "application/x-dosexec"})
		with data.policy as policy
	startswith(r, "file type not allowed")
}

test_a_nested_zip_is_refused if {
	"a ZIP inside a ZIP" in gate.file_reasons with input as object.union(pdf, {"ext": "zip", "true_type": "application/zip", "depth": 1})
		with data.policy as policy
}

test_a_clean_email_has_no_banner if {
	gate.decision == {"red_banner": false, "needs_human": false, "reasons": []} with input as clean_email
		with data.policy as policy
}

test_injection_gives_a_red_banner_and_a_human if {
	d := gate.decision with input as object.union(clean_email, {"injection": {"score": 0.99, "where": "hidden HTML text"}})
		with data.policy as policy
	d.red_banner
	d.needs_human
}

test_spoofing_and_dmarc_fail_are_reasons if {
	d := gate.decision with input as object.union(clean_email, {"sender": {"from_domain": "x.local", "dmarc": "fail", "spoof": true, "display_name_claims": "Kestrel Automation"}})
		with data.policy as policy
	count(d.reasons) == 2
}

test_an_oversized_file_is_quarantined if {
	r := gate.file_reasons with input as {"filename": "big.pdf", "ext": "pdf", "true_type": "application/pdf", "virus": null,
		"zip_problems": [], "depth": 0, "size_mb": 22.5}
		with data.policy as {"gate": {"files": {"max_file_mb": 15, "allowed_types": {"application/pdf": ["pdf"]}, "zip": {"max_depth": 1}}}}
	r == {"file is 22.5 MB, above the 15 MB limit"}
}
