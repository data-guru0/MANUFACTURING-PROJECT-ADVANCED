# Runbooks (S19)

For Apex's IT person (Vikram) and the FDE team. Everything runs locally with Docker Compose (`make` from the repository
root; `$C` below = `docker compose -p apex -f docker-compose.legacy.yml -f docker-compose.agent.yml`). Every procedure
here was written against the real stack. The drills that exercised them are in `DRILLS.md` and `RESTORE_DRILL.md`.

## 1. Start and stop
| Goal | Command | Check |
|---|---|---|
| Start the client's legacy system only | `make demo-legacy` | Odoo http://127.0.0.1:8069, Roundcube https://localhost:8443 |
| Start everything (legacy + agent) | `make demo-agent` | all containers *healthy*; review screen https://localhost:8448 |
| Stop everything (keep the data) | `make down` | `docker ps` shows no `apex-*` |
| Start over from nothing (**deletes all data**) | `make demo-reset` | only for class; take a backup first (§3) |

## 2. Is it healthy?
- `$C ps` (every service *healthy* or *Up*), Grafana https://localhost:8444 → **Apex · Operations** (queue age, errors).
- The review screen's queue shows a red **Manual mode** banner when the AI is off or a service is down.
- `make ai-status`: the kill switch and each agent's LiteLLM key. `make audit-verify`: the audit chain.

## 3. Back up, and restore
- **Back up:** `uv run python -m scripts.backup` writes `backups/<time>/`: every database of both Postgres instances, Odoo's filestore (drawings live there, not in the database) and the MinIO bucket, plus `manifest.json`. Copy that folder off the machine. Back up `.env` **separately and encrypted**: it holds every secret.
- **Prove the backup:** `uv run python -m scripts.restore_drill` restores the latest backup into throwaway containers and checks counts, the audit chain and every content-addressed file. Do it after every backup that matters; a backup you never restored is a hope.
- **Restore for real** (the live system is broken or wrong):
  1. `make down`; keep the broken volumes until the restore is confirmed (`docker volume ls`).
  2. `make demo-agent` on empty volumes (or `make demo-reset` without `seed`), then stop the writers: `$C stop worker ingest review-ui`.
  3. Databases: `$C exec -T app-postgres dropdb -U postgres apex && $C exec -T app-postgres createdb -U postgres -O apex apex`, then `$C exec -T app-postgres pg_restore -U postgres -d apex --no-owner < backups/<time>/app-postgres__apex.dump`; the same for `odoo-db` (`-U odoo`, `odoo-db__apex.dump`) and the other dumps.
  4. Odoo filestore: `$C exec -T odoo tar -C /var/lib/odoo -xf - < backups/<time>/odoo_filestore.tar`.
  5. MinIO: unpack `minio_apex.tar` and copy it back with the knowledge job (`Store().put`), or `mc mirror` from a machine with `mc`.
  6. `uv run python -m agent.ops.provision_access` (roles and grants), `make audit-verify`, then `$C start worker ingest review-ui`.

## 4. Roll back a bad version
- **Code / image:** before every rebuild, keep the running image: `docker tag apex/agent:local apex/agent:rollback-$(date +%Y%m%d)`. To roll back: `docker tag apex/agent:rollback-<date> apex/agent:local && $C up -d --force-recreate worker ingest review-ui whatif erp-mcp-read erp-mcp-write mail-mcp share-mcp`. The code itself: `git log`, `git checkout <commit> -- agent/`.
- **A prompt:** Langfuse → Prompts → the prompt → set the previous version to `production`, or `uv run python -m agent.ops.prompts promote <name> <version> --by <name> --evidence <eval file>`; `make prompts` must say `ok`.
- **The cycle-time model:** `… run --rm knowledge python -m apex_agent.costing.cycle_time promote <older version> --by <name>`. The gate refuses a version that isn't better on the held-out jobs; to roll back anyway, a senior writes `models/cycle_time/latest.json` back to the older version (and says why in the audit log via `make ai-off`/`ai-on` reasons or a PROGRESS note).
- **A pricing rule:** delete its entry under `pricing_rules` in `agent/config/policy.yaml`, then `make policy`. Rules are approved by a person and only ever added by `rules approve`.

## 5. Rotate a key
| Key | How | Then |
|---|---|---|
| A LiteLLM agent key (`LITELLM_KEY_*`) | empty its line in `.env`, `make secrets` | `make agent-keys` (registers the new value), `$C up -d --force-recreate <the service using it>` |
| The Groq key (`GROQ_API_KEY`) | create a new key in the Groq console, put it in `.env` yourself (never paste it anywhere else), revoke the old one | `$C up -d --force-recreate litellm` |
| The approval signing key (`APPROVAL_SIGNING_KEY`) | empty it, `make secrets` | `$C up -d --force-recreate review-ui opa`: approvals signed before become invalid (re-approve those jobs) |
| A database or MinIO password | empty it, `make secrets` | `uv run python -m agent.ops.provision_access`, then recreate the services that use it |
`.env` is never committed, printed or pasted. `make check` runs gitleaks on every check, and the pre-commit hook runs it on every commit.

## 6. A stuck job
1. Find it: review screen (queue / job page shows status and error), or `$C exec -T app-postgres psql -U apex -d apex -c "SELECT email_id, status, attempts, error, available_at FROM jobs WHERE status IN ('queued','running','failed') ORDER BY available_at"`.
2. `running` for > 10 min: the worker takes it back itself (rescue). If the worker is down: `$C start worker`.
3. `queued` with an outage error (AiUnavailable, *Odoo unreachable*, Quota): it waits by design and resumes; fix the outage (§7).
4. `failed`: read the error; after fixing the cause `… exec -T worker python -m apex_agent.worker --retry-failed`, or re-run one job from the estimate: `--rerun <email_id>`.
5. Can't wait: hand it to a person: `… exec -T worker python -m apex_agent.worker --to-people <email_id> --why "<reason>"` (status `manual`; quote it the legacy way).

## 7. An outage (Groq, Odoo, a tool server)
- Groq or LiteLLM down: jobs wait (*Manual mode* banner), nothing is lost, they resume when it's back. Check `$C logs litellm`, the egress proxy (`$C ps envoy-egress`), the Groq status page. A long outage: hand the urgent backlog to people (§6.5).
- Odoo down: jobs wait at costing (*Odoo unreachable*); no price is guessed. `$C start odoo` / `$C logs odoo`.
- The Groq daily quota spent: jobs wait 30 min at a time (QuotaExhausted); the what-if assistant falls back to the smaller model.

## 8. A suspected prompt injection
1. **Stop the AI:** `make ai-off BY=<name> WHY="suspected injection"`. Every AI step is refused (OPA) and every agent key is blocked (LiteLLM); the screen shows Manual mode; nothing is sent.
2. Find it: the red banner on the job (S3 gate), Grafana → Apex · AI Quality, Langfuse traces of that job, `make audit-verify` (has anything been changed?).
3. Contain: the email's attachments are already in `quarantine/` if the gate stopped them; reject the job on the screen with a reason.
4. Check the guard still holds: `make redteam` (opt-in, uses Groq).
5. `make ai-on BY=<name> WHY="<what was found and fixed>"`. Record it in `docs/governance/` (the incident runbook).

## 9. The kill switch
`make ai-off BY=… WHY=…` / `make ai-on BY=… WHY=…` / `make ai-status`. Both are written to the audit log with who and why. Off means **manual mode**: the legacy process (Odoo, Roundcube, the Excel calculator) keeps working.
