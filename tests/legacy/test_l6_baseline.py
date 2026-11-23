"""L6 Done-when: the baseline report renders from the live system, and every weakness W1-W18 has
evidence. Runs against the live mail server, Odoo, the file share and the generated reports."""

import re
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.baseline_metrics import MARKER, REPORT, Baseline, mailbox_times, measure
from scripts.pain_points import REPORT as PAIN
from scripts.pain_points import TITLE

REPO = Path(__file__).resolve().parents[2]
SOP = REPO / "docs" / "legacy" / "SOP_MANUAL_QUOTING.md"


def run(module: str) -> str:
    r = subprocess.run([sys.executable, "-m", module], cwd=REPO, capture_output=True, text=True, timeout=600,
                       encoding="utf-8", errors="replace")  # fmt: skip
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture(scope="module")
def b() -> Baseline:
    return measure()


def test_mailbox_history_is_in_imap_and_agrees_with_odoo(b: Baseline) -> None:
    assert len(mailbox_times("Archive")) == 400 and len(mailbox_times("Sent")) == 400
    assert len(b.response_days) == 400
    assert b.imap_matches_odoo == 1.0, "mail timestamps and Odoo's received/sent fields disagree"


def test_baseline_numbers_are_in_the_expected_ranges(b: Baseline) -> None:
    days = list(b.response_days.values())
    assert 3 <= statistics.median(days) <= 6, "W1: replies take 3-5 days"
    assert 0.25 <= b.win_rate <= 0.30
    assert 0.62 <= b.on_time / b.delivered <= 0.78, "W7: ~70 % on time"
    assert 0.15 <= b.lost_with_reason / b.lost <= 0.25, "W9: ~20 % with a reason"
    assert len(b.loss_jobs) >= 8, "W8/W11/W14: loss-making jobs exist"
    big = [(n, w) for _, n, w in b.by_bucket if n >= 20]
    assert big[0][1] / big[0][0] > big[-1][1] / big[-1][0], "faster replies should win more often"


def test_report_renders_and_keeps_the_students_log() -> None:
    run("scripts.baseline_metrics")
    text = REPORT.read_text(encoding="utf-8")
    for heading in ("Median response time", "p90 response time", "Win rate", "On-time delivery",
                    "Lost quotes with a known reason", "Loss-making jobs", "Win rate by response time"):  # fmt: skip
        assert heading in text
    assert MARKER in text and "Students' hand-quoting log" in text
    sentinel = "| test-sentinel | 12 | 34 | 56 |"
    REPORT.write_text(text + sentinel + "\n", encoding="utf-8", newline="\n")
    try:
        run("scripts.baseline_metrics")
        assert sentinel in REPORT.read_text(encoding="utf-8"), "regenerating must keep what students typed"
    finally:
        REPORT.write_text(REPORT.read_text(encoding="utf-8").replace(sentinel + "\n", ""), encoding="utf-8", newline="\n")


def test_every_weakness_has_live_evidence() -> None:
    run("scripts.pain_points")
    rows = {m.group(1): m.group(2) for m in re.finditer(r"^\| \*\*(W\d+)\*\* \| [^|]+ \| ([^|]+) \|", PAIN.read_text(
        encoding="utf-8"), re.M)}  # fmt: skip
    assert set(rows) == set(TITLE) and len(rows) == 18
    for w, ev in rows.items():
        assert re.search(r"\d", ev), f"{w}: evidence should carry a measured number: {ev}"


def test_sop_screenshots_exist() -> None:
    images = re.findall(r"!\[[^\]]*\]\((img/[^)]+)\)", SOP.read_text(encoding="utf-8"))
    assert len(images) >= 8
    for rel in images:
        path = SOP.parent / rel
        assert path.exists() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and path.stat().st_size > 10_000, rel
