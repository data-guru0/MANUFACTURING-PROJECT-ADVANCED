package apex.extraction_test

import rego.v1

import data.apex.extraction

test_plain_rfq_goes_to_an_estimator if {
	extraction.route == "estimator" with input as {"export_control": null}
}

test_itar_goes_to_a_senior if {
	extraction.route == "senior" with input as {"export_control": "ITAR (USML Category XI)"}
}

test_ear_goes_to_a_senior if {
	extraction.route == "senior" with input as {"export_control": "EAR (ECCN 9A610)"}
}
