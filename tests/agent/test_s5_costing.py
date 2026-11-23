"""S5 Done-when, against the live stacks and real data:
- the median absolute % error against held-out history is reported (target about 12 %);
- the committed date is met in most historical cases in a backtest (report the %);
- a `hypothesis` test proves that a tighter tolerance never lowers the price and that a higher quantity never
  raises the unit price;
- CO2 is reproducible by hand for one part;
- the commodity index is fetched for real and stored.
Plus: every cost line has a provenance, no LLM in the costing code, and the egress allow-list blocks other hosts."""

import json
import os
import subprocess
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault("APEX_CONFIG_DIR", str(REPO / "agent" / "config"))
from apex_agent.costing import cycle_time  # noqa: E402
from apex_agent.costing.engine import Context, estimate  # noqa: E402
from apex_agent.costing.quote import build_context  # noqa: E402
from apex_agent.costing.standards import Part, shop  # noqa: E402

COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(REPO / "docker-compose.legacy.yml"), "-f",
           str(REPO / "docker-compose.agent.yml")]  # fmt: skip
MATERIALS = list(yaml.safe_load((REPO / "agent/config/costing.yaml").read_text(encoding="utf-8"))["times"]["mrr_cm3_per_min"])
OSP = ["OSP-ANODIZE", "OSP-PLATING", "OSP-POWDER", "OSP-PASSIVATE", "OSP-HEATTREAT"]


def in_worker(code: str) -> Any:
    r = subprocess.run(
        [*COMPOSE, "exec", "-T", "worker", "python", "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=300
    )
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def sql(query: str) -> list[list[str]]:
    r = subprocess.run([*COMPOSE, "exec", "-T", "app-postgres", "psql", "-U", "postgres", "-d", "apex", "-At", "-F", "\t", "-c", query],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)  # fmt: skip
    assert r.returncode == 0, r.stderr
    return [line.split("\t") for line in r.stdout.splitlines()]


@cache
def live_context() -> Context:
    """The REAL inputs: live Odoo facts through erp-mcp-read (as the worker gets them), the real training rows and the
    deployed model's card (fill factors, P90 calibration). The model is refit here from those same rows."""
    facts = in_worker(f"import json; from apex_agent.costing.quote import fetch_facts; from apex_agent.settings import load; "
                      f"print(json.dumps(fetch_facts(load().opa_url, {MATERIALS!r}, {OSP!r}, None), default=str))")  # fmt: skip
    rows = in_worker("import json; from apex_agent.costing.cycle_time import training_rows; from apex_agent.knowledge import db; "
                     "print(json.dumps(training_rows(db.connect())))")  # fmt: skip
    card = in_worker("import json; from apex_agent.costing.cycle_time import load; print(json.dumps(load().card))")
    model = cycle_time.fit([r for r in rows if not cycle_time.held_out(r["part"])])
    model.card = card
    return build_context(facts, date.today(), model)


FAMILY = st.sampled_from(sorted(shop()["families"]))
PART = st.builds(
    lambda fam, mat, l_, w, h, holes, threads, pockets, bends, ra, osp: Part(fam, mat, l_, w, h if fam != "sheet_panel" else min(h, 6.0),
                                                                             holes, threads, pockets, bends, 0.1, ra, osp),
    FAMILY, st.sampled_from(MATERIALS), st.floats(10, 600), st.floats(5, 300), st.floats(1, 150), st.integers(0, 20),
    st.integers(0, 8), st.integers(0, 4), st.integers(0, 4), st.sampled_from([0.4, 0.8, 1.6, 3.2]),
    st.lists(st.sampled_from(OSP), max_size=2, unique=True),
)  # fmt: skip
TOL = st.sampled_from([0.002, 0.005, 0.01, 0.02, 0.05, 0.1])
SETTINGS = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(part=PART, t1=TOL, t2=TOL, qty=st.integers(1, 2000))
def test_a_tighter_tolerance_never_lowers_the_price(part: Part, t1: float, t2: float, qty: int) -> None:
    tight, loose = sorted((t1, t2))
    ctx = live_context()
    p_tight = estimate(Part(**{**part.__dict__, "tolerance_mm": tight}), qty, ctx).unit_price
    p_loose = estimate(Part(**{**part.__dict__, "tolerance_mm": loose}), qty, ctx).unit_price
    assert p_tight >= p_loose, (part, tight, loose, qty, p_tight, p_loose)


@SETTINGS
@given(part=PART, tol=TOL, q1=st.integers(1, 3000), q2=st.integers(1, 3000))
def test_a_higher_quantity_never_raises_the_unit_price(part: Part, tol: float, q1: int, q2: int) -> None:
    few, many = sorted((q1, q2))
    p = Part(**{**part.__dict__, "tolerance_mm": tol})
    ctx = live_context()
    assert estimate(p, many, ctx).unit_price <= estimate(p, few, ctx).unit_price, (p, few, many)


def test_every_cost_line_has_a_provenance_and_no_llm_prices() -> None:
    ctx = live_context()
    e = estimate(Part("l_bracket", "MAT-AL6061", 120, 40, 60, holes=4, threads=2, tolerance_mm=0.02), 50, ctx)
    assert e.lines and all(ln.provenance for ln in e.lines)
    assert abs(sum(ln.amount for ln in e.lines) - e.unit_price) < 0.01  # the lines add up to the price
    for f in (REPO / "agent/src/apex_agent/costing").glob("*.py"):  # the LLM never prices: no costing module calls a model
        code = f.read_text(encoding="utf-8")
        assert "LLMClient" not in code and ".chat(" not in code, f.name


def test_co2_is_reproducible_by_hand() -> None:
    (res,), *_ = sql("SELECT result FROM estimates ORDER BY created_at LIMIT 1")
    e = json.loads(res)
    f = yaml.safe_load((REPO / "agent/config/emission_factors.yaml").read_text(encoding="utf-8"))
    part, kg = e["part"], e["breaks"][0]["lines"][0]["detail"]["kg"]
    scrap = shop()["stock"]["scrap_factor"][shop()["families"][part["family"]]["process"]]
    wcs = {w["code"]: w for w in live_context().workcenters.values()}
    by_hand = (kg * scrap * f["materials"][part["material"]]["factor"] if f["materials"][part["material"]]["factor"] else 0.0) \
        + sum(m[1] / 60 * float(wcs[wc]["x_power_kw"] or 0) for wc, m in e["minutes_per_part"].items()) * f["grid"]["factor"]  # fmt: skip
    assert abs(by_hand - e["co2"]["per_part_kg"]) <= 0.01 * max(by_hand, 0.01) + 0.002, (by_hand, e["co2"])
    assert "not a certified product carbon footprint" in e["co2"]["methodology"].lower()


def test_the_commodity_index_was_fetched_for_real_and_stored() -> None:
    rows = sql("SELECT series, count(*), max(month), min(source_url) FROM commodity_index GROUP BY series ORDER BY series")
    assert {r[0] for r in rows} >= {"Aluminum", "Copper", "Nickel"}
    for _series, n, latest, url in rows:
        assert int(n) > 600 and latest >= "2026-01-01" and url.startswith("https://thedocs.worldbank.org/")
    (res,), *_ = sql("SELECT result FROM estimates WHERE result->'terms'->'index' <> 'null'::jsonb LIMIT 1")
    t = json.loads(res)["terms"]
    assert t["clause"] and str(t["index"]["value"]).split(".")[0] in t["clause"].replace(",", "")


def test_egress_only_allows_the_listed_hosts() -> None:
    probe = ("import httpx\nfor url in ('https://thedocs.worldbank.org/', 'https://example.com/'):\n"
             "    try:\n        print(url, httpx.head(url, timeout=20, trust_env=True).status_code)\n"
             "    except httpx.ProxyError as e:\n        print(url, 'blocked', e)\n")  # fmt: skip
    r = subprocess.run(
        [*COMPOSE, "run", "--rm", "-T", "knowledge", "python", "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert "https://example.com/ blocked" in r.stdout and "403" in r.stdout, r.stdout + r.stderr[-500:]


def test_price_error_and_delivery_backtest_are_reported() -> None:
    r = subprocess.run([*COMPOSE, "run", "--rm", "-T", "knowledge", "python", "-m", "apex_agent.costing.evaluate"],
                       capture_output=True, text=True, encoding="utf-8", timeout=600)  # fmt: skip
    assert r.returncode == 0, r.stderr[-2000:]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    print(f"\ncosting: {res}")
    assert res["price_quotes"] >= 50 and res["price_mdape"] is not None  # target ≈ 12 %: measured and reported
    assert res["backtest_orders"] >= 50 and res["committed_met"] is not None
    report = (REPO / "docs/fde/COSTING_EVAL.md").read_text(encoding="utf-8")
    assert "## 1. Price" in report and "## 3. Delivery date backtest" in report and "## 4. CO2 for one part, by hand" in report
