# Resilience drills (S19)

_Run by `uv run python -m scripts.drills …` against the live local stack. Raw results: `drills.json`._

## groq-down: PASSED (2026-11-23T22:52)

- job: `19`
- waiting_while_down: `true`
- error_seen: `"AiUnavailable: LiteLLM error 500: {\"error\":{\"message\":\"litellm.InternalServerError: InternalServerError: GroqException - Cannot connect to host envoy-egress:100"`
- attempts: `[0, 0]`
- manual_mode_banner: `true`
- job_page_served: `true`
- finished_after_restore: `true`
- minutes: `2.4`

## odoo-down: PASSED (2026-11-23T22:54)

- job: `12`
- waiting_while_down: `true`
- error_seen: `"NeedsHuman: erp-mcp-read list_workcenters: Error executing tool list_workcenters: Odoo unreachable: ConnectError"`
- attempts: `[0, 0]`
- manual_mode_banner: `true`
- job_page_served: `true`
- finished_after_restore: `true`
- minutes: `2.5`

## worker-kill: PASSED (2026-11-23T22:55)

- job: `15`
- killed_after_nodes: `"security_gate,triage,extract,repeat,reconcile,retrieve,capability,cost_part,suppliers,risk_policy"`
- nodes_run: `["security_gate:1", "triage:1", "extract:1", "repeat:1", "reconcile:1", "retrieve:1", "capability:1", "cost_part:1", "suppliers:1", "risk_policy:1", "draft_reply:1"]`
- finished_nodes_run_twice: `[]`
- finished_after_restart: `true`

## burst: PASSED (2026-11-23T22:57)

- sent: `100`
- ingested: `100`
- all_ingested: `true`
- ingest_minutes: `0.8`
- jobs_while_ai_off: `"queued:99,running:1"`
- failed: `0`
- manual_mode_banner: `true`
- handed_to_people: `{"handed_to_people": 100, "left_in_queue": 0}`
