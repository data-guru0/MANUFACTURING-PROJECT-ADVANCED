"""S19 Done-when: the drills have been run and recorded, and a restore has worked (reads the recorded results; the
drills and the restore themselves are run by scripts/drills.py and scripts/restore_drill.py, see DEMO.md S19)."""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNBOOKS = REPO / "docs" / "runbooks"


def test_every_drill_was_run_and_passed() -> None:
    results = json.loads((RUNBOOKS / "drills.json").read_text(encoding="utf-8"))
    assert set(results) == {"groq-down", "odoo-down", "worker-kill", "burst"}, sorted(results)
    assert all(r["passed"] for r in results.values()), {k: r for k, r in results.items() if not r["passed"]}


def test_a_restore_has_worked() -> None:
    text = (RUNBOOKS / "RESTORE_DRILL.md").read_text(encoding="utf-8")
    assert "**Result: PASSED**" in text and "mismatches: **0**" in text and "audit chain verifies" in text


def test_the_quality_gate_has_a_pre_commit_hook() -> None:
    cfg = (REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert all(h in cfg for h in ("ruff", "mypy", "opa", "gitleaks"))
    assert (REPO / ".git" / "hooks" / "pre-commit").exists(), "run: uv run pre-commit install"
