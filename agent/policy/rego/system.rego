# The AI kill switch (S1; S9 adds approval, escalation, actions and tools policies).
# Every AI step asks OPA `apex/system/ai_enabled` first. It fails closed: missing or
# non-true policy data means "no", and an unreachable OPA means "no" in the client too.
package apex.system

import rego.v1

default ai_enabled := false

ai_enabled if data.policy.ai_enabled == true
