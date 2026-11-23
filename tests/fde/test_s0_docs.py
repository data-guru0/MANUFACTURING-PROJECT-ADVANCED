"""S0 Done-when (machine-checkable part): the discovery and design documents exist, every weakness has
a fix, a phase and a metric, the baseline numbers quoted match the measured report, and every link
resolves. (The other half of Done-when is the human approving the scope.)"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"
REQUIRED = ["fde/DISCOVERY.md", "fde/SOLUTION_DESIGN.md", "fde/SOW.md", "security/THREAT_MODEL.md", "ARCHITECTURE.md"]
ADRS = [
    "graph-not-free-agent",
    "postgres-queue",
    "mcp-least-privilege",
    "opa-decisions-yaml-data",
    "litellm-only-ai-gateway",
    "envoy-egress",
]  # fmt: skip  (the six decisions named in 02 §2)


def read(rel: str) -> str:
    return (DOCS / rel).read_text(encoding="utf-8")


def test_documents_and_adrs_exist() -> None:
    for rel in REQUIRED:
        assert (DOCS / rel).stat().st_size > 1000, rel
    names = [p.name for p in (DOCS / "adr").glob("*.md")]
    for slug in ADRS:
        assert any(slug in n for n in names), f"missing ADR: {slug}"


def test_every_weakness_has_a_fix_a_phase_and_a_metric() -> None:
    rows = dict(re.findall(r"^\| \*\*(W\d+)\*\* \|(.+)$", read("fde/SOLUTION_DESIGN.md"), re.M))
    assert sorted(rows, key=lambda w: int(w[1:])) == [f"W{i}" for i in range(1, 19)]
    for w, row in rows.items():
        fix, phase, metric = [c.strip() for c in row.split("|")[:3]]
        assert len(fix) > 20, w
        assert re.fullmatch(r"S\d+(–S\d+)?(, S\d+)*", phase), f"{w}: phase {phase!r}"
        assert all(0 <= int(n) <= 20 for n in re.findall(r"S(\d+)", phase)), w
        assert re.search(r"\d", metric), f"{w}: the success metric needs a number"


def test_quoted_baseline_matches_the_measured_report() -> None:
    baseline = read("legacy/BASELINE_METRICS.md")
    design = read("fde/SOLUTION_DESIGN.md") + read("fde/DISCOVERY.md")
    # The S0 documents record the baseline measured on 2026-11-20. The synthetic history is regenerated relative to the
    # day of each `make demo-reset`, and `make baseline` re-measures it, so the numbers drift a little between reset
    # days: the design must quote a value CLOSE to today's measurement (3 points, or 1 day), not a different story.
    for label in ("Median response time", "Win rate", "On-time delivery", "Lost quotes with a known reason"):
        measured = re.search(rf"{label}[^|]*\| \*\*([\d.]+) ?(days|%)", baseline)
        assert measured, label
        value, unit = float(measured.group(1)), measured.group(2)
        quoted = [float(x) for x in re.findall(rf"(\d+(?:\.\d+)?) ?{unit}", design)]
        tolerance = 1.0 if unit == "days" else 3.0
        assert any(abs(q - value) <= tolerance for q in quoted), f"{label}: no quoted value near the measured {value} {unit}"


def test_threat_model_covers_owasp_llm_top_10_and_stride() -> None:
    tm = read("security/THREAT_MODEL.md")
    for i in range(1, 11):
        assert f"| LLM{i:02d} |" in tm
    for letter in ("**S**poofing", "**T**ampering", "**R**epudiation", "**I**nformation", "**D**enial", "**E**levation"):
        assert letter in tm


def test_relative_links_resolve() -> None:
    files = [DOCS / r for r in REQUIRED] + list((DOCS / "adr").glob("*.md"))
    for f in files:
        for target in re.findall(r"\]\(([^)#]+\.md)\)", f.read_text(encoding="utf-8")):
            if not target.startswith("http"):
                assert (f.parent / target).resolve().exists(), f"{f.name}: broken link {target}"
