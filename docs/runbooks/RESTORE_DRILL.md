# Restore drill (S19)

_Run 2026-11-23T18:53 by `uv run python -m scripts.restore_drill` on backup `20261123-185136` (taken 2026-11-23T18:51:36), into throwaway containers with no network; the live system was not touched._

**Result: PASSED** in 1.3 min of restoring.

- Backup files unchanged since the backup (sha256): **True**
- `odoo-db:apex` restored: counts 404 (expected 404)
- `app-postgres:apex` restored: counts 34/22/2270 (expected 34/22/2270); audit chain verifies over 2270 rows
- `app-postgres:keycloak` restored: counts - (expected -)
- `app-postgres:langfuse` restored: counts - (expected -)
- `app-postgres:litellm` restored: counts - (expected -)
- Odoo filestore: 2070 files
- MinIO: 92 objects (expected 92); 73 content-addressed objects re-hashed, mismatches: **0**

How to restore for real: `docs/runbooks/RUNBOOKS.md` → *Restore from a backup*.
