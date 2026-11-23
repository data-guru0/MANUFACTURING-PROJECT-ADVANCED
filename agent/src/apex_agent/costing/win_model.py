"""Win probability (S14, W6: pricing by gut feel). A logistic regression on Apex's won/lost quote history, split BY TIME.

Features (all known when a quote is written):
    log(price / engine price)       the price against the engine's price for that customer's tier, with the supplier
                                    prices valid ON THE QUOTE DATE (Odoo price lists): how far the quote strayed
    response days                   RFQ received -> quote sent (Odoo x_rfq_received_at / x_quote_sent_at)
    tier A / tier C                 the customer's tier (Odoo partner tag), B = base
    log(quantity)

Trained on the oldest 75 % of decided quotes, scored on the newest 25 % (Brier score vs the constant "average win rate"
baseline, and a calibration table). Stored in MinIO (models/win/latest.json) only when it beats the baseline.
The model is small (6 numbers): prediction is plain Python, no sklearn at run time.

Usage (knowledge job): python -m apex_agent.costing.win_model train
"""

import asyncio
import dataclasses
import json
import math
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

PREFIX = "models/win"
FEATURES = ["log_price_ratio", "response_days", "tier_A", "tier_C", "log_qty"]


def features(unit_price: float, ref_price: float, qty: int, tier: str | None, response_days: float) -> list[float]:
    # capped at +-50 %: the training data's ratio has heavy tails (sd 0.39 vs ~0.10 of gut-feel noise) that swamp a linear fit
    return [
        max(-0.5, min(0.5, math.log(unit_price / ref_price))),
        float(response_days),
        float(tier == "A"),
        float(tier == "C"),
        math.log(max(qty, 1)),
    ]


def predict(m: dict[str, Any], x: list[float]) -> float:
    z = m["intercept"] + sum(c * (v - mu) / sd for c, v, mu, sd in zip(m["coef"], x, m["mean"], m["std"], strict=True))
    return 1.0 / (1.0 + math.exp(-z))


def curve(m: dict[str, Any], unit_cost: float, qty: int, tier: str | None, response_days: float, floor_pct: float,
          ceiling_pct: float, quoted_price: float) -> dict[str, Any]:  # fmt: skip
    """`quoted_price`: the engine's price (the reference the model compares every candidate price with)."""
    """Margin -> P(win) -> expected profit (P x margin $ x qty), from the floor to the ceiling, 1-point steps. Advisory."""
    pts = []
    for m_pct in range(int(math.ceil(floor_pct)), int(ceiling_pct) + 1):
        price = round(unit_cost * (1 + m_pct / 100), 2)
        p = predict(m, features(price, quoted_price, qty, tier, response_days))
        pts.append(
            {"margin_pct": m_pct, "price": price, "p_win": round(p, 4), "exp_profit": round(p * (price - unit_cost) * qty, 2)}
        )
    best = max(pts, key=lambda x: x["exp_profit"])
    p_q = predict(m, features(quoted_price, quoted_price, qty, tier, response_days))
    return {"points": pts, "best": best, "quoted": {"price": quoted_price, "margin_pct": round(100 * (quoted_price / unit_cost - 1), 1),
            "p_win": round(p_q, 4), "exp_profit": round(p_q * (quoted_price - unit_cost) * qty, 2)},
            "model": m.get("version"), "note": "advisory: the engine's price stands; OPA enforces the margin floor and ceiling"}  # fmt: skip


def load(store: Any = None) -> dict[str, Any] | None:
    """The validated model (latest.json: it beat the baseline), else the newest candidate marked NOT validated (shown
    on the margin curve with a warning, never used for the queue priority), else None."""
    from apex_agent.ingest.store import Store

    store = store or Store()
    for key, validated in (("latest.json", True), ("candidate.json", False)):
        try:
            return {**json.loads(store.get(f"{PREFIX}/{key}")), "validated": validated}
        except Exception:  # noqa: BLE001 (no such object)
            continue
    return None


# ---- training (knowledge job; no LLM)
def dataset() -> list[dict[str, Any]]:
    """One row per decided historical quote line: the features above, its date and won/lost."""
    from apex_agent.costing import cycle_time
    from apex_agent.costing.engine import estimate, part_from_spec
    from apex_agent.costing.quote import _tool, build_context, fetch_facts
    from apex_agent.costing.repeat import approved_spec
    from apex_agent.knowledge import db

    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT id, data FROM odoo_records WHERE model = 'sale.order' AND data->>'state' IN ('sale', 'cancel')")
        orders = {i: d for i, d in cur.fetchall()}
        cur.execute("SELECT data FROM odoo_records WHERE model = 'sale.order.line'")
        lines = [d for (d,) in cur.fetchall() if d["order_id"] and d["order_id"][0] in orders]
        cur.execute("SELECT id, data->>'ref' FROM odoo_records WHERE model = 'res.partner'")
        refs = dict(cur.fetchall())
    specs: dict[str, Any] = {}
    for ln in lines:
        code = ln["product_id"][1].split("]")[0].lstrip("[") if ln["product_id"] else ""
        if code and code not in specs:
            fam, f = approved_spec(conn, code)
            specs[code] = part_from_spec(f, fam) if f else None
    parts = {c: p for c, p in specs.items() if p}
    opa_url = os.environ.get("APEX_OPA_URL", "http://opa:8181")
    facts = fetch_facts(
        opa_url, sorted({p.material for p in parts.values()}), sorted({o for p in parts.values() for o in p.outside}), None
    )
    model = cycle_time.load()
    contexts: dict[date, Any] = {}  # one per quote date: the supplier prices valid that day

    async def tiers() -> dict[str, Any]:
        return {r: (await _tool("get_customer", {"ref": r})).get("tier") for r in sorted({v for v in refs.values() if v})}

    tier = asyncio.run(tiers())
    rows = []
    for ln in lines:
        o = orders[ln["order_id"][0]]
        code = ln["product_id"][1].split("]")[0].lstrip("[")
        part, qty = parts.get(code), int(ln["product_uom_qty"] or 0)
        if not part or qty < 1 or not o.get("x_rfq_received_at") or not o.get("x_quote_sent_at") or not ln["price_unit"]:
            continue
        on = datetime.fromisoformat(o["x_quote_sent_at"]).date()
        if on not in contexts:
            contexts[on] = build_context(facts, on, model)
        t = tier.get(refs.get(o["partner_id"][0]) or "")
        ref = estimate(part, qty, dataclasses.replace(contexts[on], customer={"tier": t} if t else {})).unit_price
        days = (
            datetime.fromisoformat(o["x_quote_sent_at"]) - datetime.fromisoformat(o["x_rfq_received_at"])
        ).total_seconds() / 86400
        rows.append({"order": o["name"], "date": o["date_order"], "won": o["state"] == "sale", "code": code,
                     "x": features(float(ln["price_unit"]), ref, qty, t, days)})  # fmt: skip
    return sorted(rows, key=lambda r: r["date"])


def brier(p: list[float], y: list[bool]) -> float:
    return sum((pi - yi) ** 2 for pi, yi in zip(p, y, strict=True)) / len(y)


def calibration(p: list[float], y: list[bool], bins: int = 5) -> list[dict[str, Any]]:
    """Equal-count bins: mean predicted vs observed win rate."""
    order = sorted(range(len(p)), key=lambda i: p[i])
    out = []
    for b in range(bins):
        idx = order[b * len(p) // bins : (b + 1) * len(p) // bins]
        if idx:
            out.append({"n": len(idx), "predicted": round(sum(p[i] for i in idx) / len(idx), 3),
                        "observed": round(sum(y[i] for i in idx) / len(idx), 3)})  # fmt: skip
    return out


def svg(cal: list[dict[str, Any]]) -> str:
    """The calibration plot as a small SVG (no plotting library): the diagonal is perfect calibration."""
    w, pad = 320, 40

    def s(v: float) -> float:
        return pad + v * (w - 2 * pad)

    dots = "".join(
        f'<circle cx="{s(c["predicted"]):.1f}" cy="{w - s(c["observed"]):.1f}" r="{3 + c["n"] / 8:.1f}" fill="#2563eb"/>'
        for c in cal
    )
    line = " ".join(f"{s(c['predicted']):.1f},{w - s(c['observed']):.1f}" for c in cal)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{w}" font-family="sans-serif" font-size="11">'
            f'<rect width="{w}" height="{w}" fill="white"/><line x1="{s(0)}" y1="{w - s(0)}" x2="{s(1)}" y2="{w - s(1)}" stroke="#999" stroke-dasharray="4"/>'
            f'<polyline points="{line}" fill="none" stroke="#2563eb"/>{dots}'
            f'<text x="{w / 2 - 40}" y="{w - 8}">predicted P(win)</text><text x="4" y="14">observed win rate</text></svg>')  # fmt: skip


def train() -> dict[str, Any]:
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    from apex_agent.ingest.store import Store

    rows = dataset()
    cut = int(len(rows) * 0.75)
    tr, te = rows[:cut], rows[cut:]
    X = np.array([r["x"] for r in tr])
    mean, std = X.mean(axis=0), X.std(axis=0)
    std[std == 0] = 1.0
    yt = [r["won"] for r in tr]
    # regularisation chosen INSIDE the training period (forward split: oldest 75 % of train -> newest 25 %); the test set
    # is scored once, after the choice
    v = int(len(tr) * 0.75)
    Z = (X - mean) / std

    def val_brier(c: float) -> float:
        f = LogisticRegression(C=c, max_iter=1000).fit(Z[:v], yt[:v])
        return brier(list(f.predict_proba(Z[v:])[:, 1]), yt[v:])

    c_best = min((0.01, 0.03, 0.1, 0.3, 1.0), key=val_brier)
    clf = LogisticRegression(C=c_best, max_iter=1000).fit(Z, yt)
    m = {"coef": clf.coef_[0].tolist(), "intercept": float(clf.intercept_[0]), "mean": mean.tolist(), "std": std.tolist(),
         "features": FEATURES}  # fmt: skip
    y = [r["won"] for r in te]
    p = [predict(m, r["x"]) for r in te]
    base_rate = sum(r["won"] for r in tr) / len(tr)
    b_model, b_base = brier(p, y), brier([base_rate] * len(y), y)
    version = datetime.now(UTC).strftime("v%Y%m%d%H%M%S")
    card: dict[str, Any] = {"version": version, "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "train": {"n": len(tr), "from": tr[0]["date"], "to": tr[-1]["date"], "win_rate": round(base_rate, 3)},
            "test": {"n": len(te), "from": te[0]["date"], "to": te[-1]["date"], "win_rate": round(sum(y) / len(y), 3)},
            "brier": {"model": round(b_model, 4), "baseline_average_win_rate": round(b_base, 4),
                      "skill_pct": round(100 * (1 - b_model / b_base), 1)},
            "calibration": calibration(p, y), "coefficients": dict(zip(FEATURES, [round(c, 3) for c in m["coef"]], strict=True)),
            "beats_baseline": b_model < b_base, "C": c_best}  # fmt: skip
    m.update(version=version, card=card)
    store = Store()
    store.put(f"{PREFIX}/{version}.json", json.dumps(m).encode(), "application/json")
    store.put(f"{PREFIX}/candidate.json", json.dumps(m).encode(), "application/json")
    if card["beats_baseline"]:  # the gate: a model that doesn't beat "everyone wins 26 %" is never used
        store.put(f"{PREFIX}/latest.json", json.dumps(m).encode(), "application/json")
    docs = Path(os.environ.get("APEX_DOCS_DIR", "/app/docs-fde"))
    (docs / "win_calibration.svg").write_text(svg(card["calibration"]), encoding="utf-8")
    (docs / "WIN_MODEL_CARD.md").write_text("\n".join([
        f"# Model card: win probability ({version})", "",
        "_Generated by `python -m apex_agent.costing.win_model train` (S14). No LLM._", "",
        "**Use:** P(win) of a quote at a given price, for the margin curve (advisory) and the queue priority. "
        "**Not for:** setting prices automatically (the engine prices; OPA enforces the margin floor and ceiling).", "",
        "**Model:** logistic regression on 5 features: " + ", ".join(FEATURES) + ".", "",
        f"**Data:** Apex's decided quotes in Odoo, split by time. Train: {card['train']['n']} quotes "
        f"({card['train']['from'][:10]} to {card['train']['to'][:10]}, win rate {card['train']['win_rate']:.1%}). "
        f"Test: {card['test']['n']} quotes ({card['test']['from'][:10]} to {card['test']['to'][:10]}, win rate {card['test']['win_rate']:.1%}).", "",
        f"**Result (test):** Brier {card['brier']['model']} vs {card['brier']['baseline_average_win_rate']} for the average win rate "
        f"(skill {card['brier']['skill_pct']} %). Used: **{'yes' if card['beats_baseline'] else 'NO: does not beat the baseline'}**.", "",
        "| Bin | Quotes | Predicted | Observed |", "|---|---|---|---|",
        *[f"| {i + 1} | {c['n']} | {c['predicted']:.1%} | {c['observed']:.1%} |" for i, c in enumerate(card["calibration"])], "",
        "![calibration](win_calibration.svg)", "",
        "**Coefficients (standardised):** " + ", ".join(f"{k} {v:+}" for k, v in card["coefficients"].items()), "",
        "**Limits:** the reference price is today's engine (rates, routing, cycle-time model) with the supplier prices "
        "valid on each quote's date, not the spreadsheet Apex used then; "
        "competitors' prices are unknown; a small test set (one sample of the future), which was looked at during "
        "development (two feature fixes before the regularisation was chosen inside the training period); "
        "the model is retrained by hand (`train`), and it replaces the current one only when it beats the baseline.", ""]),
        encoding="utf-8")  # fmt: skip
    return card


if __name__ == "__main__":
    if sys.argv[1:] != ["train"]:
        raise SystemExit(__doc__)
    print(json.dumps(train(), default=str))
