# What may leave the box (S9 data governance). Every LLM call goes to Groq through LiteLLM; for an RFQ that mentions
# export control, policy.yaml -> data_governance.keep_export_controlled_local decides whether that is allowed at all.
# The worker asks BEFORE the first model call (security_gate), from a code-only keyword check. No answer = no.
package apex.data

import rego.v1

default external_llm_allowed := false

external_llm_allowed if not input.export_controlled

external_llm_allowed if {
	input.export_controlled
	data.policy.data_governance.keep_export_controlled_local == false
}
