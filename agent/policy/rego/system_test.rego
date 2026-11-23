package apex.system_test

import data.apex.system
import rego.v1

test_enabled_when_policy_says_so if system.ai_enabled with data.policy.ai_enabled as true

test_disabled_by_the_kill_switch if not system.ai_enabled with data.policy.ai_enabled as false

test_fails_closed_without_policy_data if not system.ai_enabled with data.policy as {}

test_fails_closed_on_a_non_boolean if not system.ai_enabled with data.policy.ai_enabled as "yes"
