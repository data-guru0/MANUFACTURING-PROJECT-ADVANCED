package apex.suppliers_test

import rego.v1

import data.apex.actions
import data.apex.suppliers

test_an_ordinary_job_may_ask_suppliers if suppliers.decision.allow with input as {"nda_restricted": false, "export_control": null}

test_an_nda_customer_is_blocked if {
	d := suppliers.decision with input as {"nda_restricted": true, "export_control": null}
	not d.allow
	d.deny == ["the customer is NDA-restricted: their parts and drawings never leave Apex"]
}

test_an_export_controlled_job_is_blocked if {
	d := suppliers.decision with input as {"nda_restricted": false, "export_control": "ITAR (USML Category XI)"}
	not d.allow
}

test_a_boolean_export_flag_is_blocked if not suppliers.send_allowed with input as {"nda_restricted": false, "export_control": true}

test_false_export_flag_is_not_export_control if suppliers.send_allowed with input as {"nda_restricted": false, "export_control": false}

test_missing_facts_fail_closed if not suppliers.send_allowed with input as {"nda_restricted": true}

test_a_quote_approval_cannot_send_a_supplier_rfq if {
	"a supplier RFQ needs a supplier_rfq approval" in actions.deny with input as {"tool": "mail.send_supplier_rfq", "email_id": 1, "text_sha256": "x"}
		with actions.claims as {"email_id": 1, "action": "approve", "roles": ["estimator"], "text_sha256": "x", "supplier_ok": true}
}

test_a_supplier_rfq_without_the_suppliers_check_is_refused if {
	"nothing goes to suppliers for an NDA-restricted customer or an export-controlled job" in actions.deny with input as {"tool": "mail.send_supplier_rfq", "email_id": 1, "text_sha256": "x"}
		with actions.claims as {"email_id": 1, "action": "supplier_rfq", "roles": ["estimator"], "text_sha256": "x"}
}
