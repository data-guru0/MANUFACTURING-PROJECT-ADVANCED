# Apex FDE project. Recipes are one line each so they run the same under cmd and sh.
# The legacy stack never depends on the agent stack: demo-legacy uses only the legacy file.

LEGACY = docker compose -p apex -f docker-compose.legacy.yml
AGENT  = docker compose -p apex -f docker-compose.legacy.yml -f docker-compose.agent.yml

.PHONY: redteam evals evals-record ai-off ai-on ai-status audit-verify governance-report retention supply-chain prompts secrets demo-legacy demo-agent demo-reset seed nist rfq-burst rfq-drip baseline screenshots agent-keys wheels knowledge triage-eval extract-eval costing cost-eval policy check test lint down

secrets:      ## fill empty service passwords in .env (never touches GROQ_API_KEY)
	uv run python scripts/gen_secrets.py

demo-legacy:  ## start the client's legacy system only (mail certs + accounts must exist before the mail server starts)
	uv run python -m legacy.mail.setup_mail --prestart
	$(LEGACY) up -d --wait --build

demo-agent:   ## legacy + agent stack (S1+): certs, both stacks up and healthy, one LiteLLM key per agent, scoped accounts and (S7) the staff logins
	uv run python -m legacy.mail.setup_mail --prestart
	uv run python -c "from pathlib import Path; Path('data/generated/rfq_labels.jsonl').touch()"
	$(LEGACY) up -d --wait
	uv run python -m agent.ops.lab_dns
	$(AGENT) up -d --wait --build
	$(AGENT) restart opa grafana envoy-egress envoy-ingress
	uv run python -m agent.ops.litellm.keys
	uv run python -m agent.ops.provision_access
	uv run python -m agent.ops.prompts push

wheels:       ## lock the agent's dependencies for Linux and download the wheels on the host (the image installs offline; dkimpy is source-only and pure Python, so it is built here), plus the local embedding model (the agent containers have no internet; mounted read-only)
	uv pip compile agent/pyproject.toml --python-version 3.12 --python-platform x86_64-manylinux_2_31 --no-header -o agent/requirements.lock -q
	uvx pip wheel --quiet --no-deps -w agent/.wheels dkimpy==1.1.8
	uvx pip download --quiet --only-binary=:all: --no-deps --find-links agent/.wheels --python-version 3.12 --implementation cp --platform manylinux_2_31_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 --platform any -d agent/.wheels -r agent/requirements.lock
	uv run --no-project --with fastembed==0.9.1 python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='data/generated/models/fastembed'); print('embedding model: BAAI/bge-small-en-v1.5 in data/generated/models/fastembed')"

knowledge:    ## S2: sync Odoo + share into the read model, then write docs/fde/DATA_QUALITY.md (idempotent)
	$(AGENT) run --rm knowledge python -m apex_agent.knowledge.sync
	$(AGENT) run --rm knowledge python -m apex_agent.knowledge.report

triage-eval:  ## S3: wait for the worker to finish the queue, then score triage + the security gate against the labels
	$(AGENT) run --rm worker python -m apex_agent.worker --drain
	$(AGENT) run --rm knowledge python -m apex_agent.evaluate_triage

extract-eval: ## S4: (re)extract every RFQ already triaged, then score it against the L3 ground truth -> docs/fde/EXTRACTION_EVAL.md
	$(AGENT) run --rm worker python -m apex_agent.worker --redo-extraction --drain
	$(AGENT) run --rm knowledge python -m apex_agent.documents.evaluate

costing:      ## S5: read Apex's own history drawings (part_specs), train the cycle-time model (MinIO), fetch the World Bank index
	$(AGENT) run --rm knowledge python -m apex_agent.costing.history
	$(AGENT) run --rm knowledge python -m apex_agent.costing.cycle_time train
	$(AGENT) run --rm knowledge python -m apex_agent.knowledge.commodity_index

cost-eval:    ## S5: price, machine cost and delivery backtest on held-out history, CO2 by hand -> docs/fde/COSTING_EVAL.md
	$(AGENT) run --rm knowledge python -m apex_agent.costing.evaluate

redteam:      ## S9: the red-team suite (8 attack emails, Groq-heavy: run it on its own, not every day)
	uv run pytest tests/security -v -m redteam

evals:        ## S10: score triage (live golden set), extraction, retrieval, costing, faithfulness, red team vs thresholds + last recorded scores
	uv run python -m agent.evals.run_evals

evals-record: ## S10: same, and make this run the new reference for regressions (only if it passes)
	uv run python -m agent.evals.run_evals --record

ai-off:       ## S9 KILL SWITCH: manual mode now (OPA denies every AI step, LiteLLM blocks every agent key). BY=<name> WHY=<reason>
	uv run python -m agent.ops.killswitch off --by "$(BY)" --reason "$(WHY)"

ai-on:        ## S9: AI back on. BY=<name> WHY=<reason>
	uv run python -m agent.ops.killswitch on --by "$(BY)" --reason "$(WHY)"

ai-status:    ## S9: is the AI on, and is each agent's LiteLLM key blocked?
	uv run python -m agent.ops.killswitch status

audit-verify: ## S9: walk the hash-chained audit log (exit 1 = broken, with the first broken row)
	$(AGENT) exec -T worker python -m apex_agent.audit.verify

governance-report: ## S9: AI $ per quote / won order, ROI, reviewer oversight, budget alerts -> docs/fde/GOVERNANCE_REPORT.md
	$(AGENT) run --rm knowledge python -m apex_agent.governance.report

retention:    ## S9: what the retention rules would delete (dry run); RETENTION=--apply to delete
	$(AGENT) exec -T worker python -m apex_agent.governance.retention $(RETENTION)

prompts:      ## S9: prompt/model versions in Langfuse: does the code equal `production`?
	uv run python -m agent.ops.prompts check

supply-chain: ## S9: pip-audit, bandit, trivy (agent image), SBOM (syft)
	uv run python -m scripts.supply_chain

policy:       ## reload OPA after editing agent/policy/rego/*.rego or agent/config/policy.yaml (OPA reads them at start)
	$(AGENT) restart opa

agent-keys:   ## (re)register the per-agent LiteLLM virtual keys from agent/config/agents.yaml
	uv run python -m agent.ops.litellm.keys

demo-reset:   ## wipe all volumes and the approved pricing rules, start legacy, reseed
	docker compose -p apex down -v --remove-orphans
	uv run python -m scripts.reset_rules
	uv run python -m legacy.mail.setup_mail --prestart
	$(LEGACY) up -d --wait --build
	uv run python -m legacy.odoo.setup_odoo
	uv run python -m legacy.seed.load_odoo
	uv run python -m legacy.seed.drawings
	uv run python -m legacy.seed.attach_drawings
	uv run python -m legacy.seed.build_share
	uv run python -m legacy.mail.setup_mail
	uv run python -m legacy.traffic.rfq_traffic --purchasing-history
	uv run python -m legacy.seed.mail_history

seed:         ## configure + load synthetic business data through real APIs (idempotent)
	uv run python -m legacy.odoo.setup_odoo
	uv run python -m legacy.seed.load_odoo
	uv run python -m legacy.seed.drawings
	uv run python -m legacy.seed.attach_drawings
	uv run python -m legacy.seed.build_share
	uv run python -m legacy.mail.setup_mail
	uv run python -m legacy.traffic.rfq_traffic --purchasing-history
	uv run python -m legacy.seed.mail_history

nist:         ## download the public NIST MBE PMI test drawings (internet; once)
	uv run python scripts/download_nist.py

rfq-burst:    ## L5: send the next N labelled emails to rfq@apex.local now (default 20)
	uv run python -m legacy.traffic.rfq_traffic --burst $(or $(N),20)

# the class set's message numbers (a variable: commas inside $(or …) would split its arguments)
CLASS_SET = 1,2,3,4,5,6,7,8,10,12,19,21,22,27,29,37,53,62,64,68

rfq-class:    ## S20: the CLASS SET: 20 labelled emails that cover every demo (the L5 tour, all 5 attacks on rfq@, scans, repeats, revisions, no-bids, NDA, export, 7075)
	uv run python -m legacy.traffic.rfq_traffic --only $(or $(MSGS),$(CLASS_SET))

rfq-drip:     ## L5: emails keep arriving at random (Poisson) gaps, ~30 per hour; Ctrl+C stops
	uv run python -m legacy.traffic.rfq_traffic --drip --per-hour $(or $(RATE),30)

baseline:     ## L6: measure the as-is baseline (IMAP + Odoo) and the W1-W18 evidence -> docs/legacy/*.md (+ the dashboards' 'before' numbers)
	uv run python -m scripts.baseline_metrics
	uv run python -m scripts.pain_points
	uv run python -m agent.ops.grafana.make_dashboards

screenshots:  ## L6: screenshots of the running system for the SOP, using the installed Microsoft Edge
	uv run python -m scripts.screenshots

test:
	uv run pytest -v

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

# (//p and //repo: a leading double slash stops Git Bash from rewriting container paths into Windows paths)
check:        ## the local quality gate: lint + types + tests + OPA policy tests + secret scan
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	docker run --rm -v "$(CURDIR)/agent/policy/rego:/p:ro" openpolicyagent/opa:1.22.4-static test //p -v
	docker run --rm -v "$(CURDIR):/repo:ro" zricethezav/gitleaks:v8.30.0 dir //repo --config //repo/.gitleaks.toml --redact --no-banner
	uv run python -m scripts.supply_chain
	uv run python -m agent.evals.run_evals
	uv run pytest -v

down:         ## stop everything in project "apex" (works with or without the agent layer)
	docker compose -p apex down --remove-orphans
