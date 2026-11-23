"""The cycle-time model (S5): how long does a work order REALLY take, compared with the standard minutes?

Trained on Apex's finished work orders (the S2 read model): target = log(actual / planned minutes) per work order,
features = work center, part family, material, log(lot size), log(planned minutes per part).
Three quantile models (P10, P50, P90): scikit-learn HistGradientBoostingRegressor, its gradient-boosting variant
that supports MONOTONIC constraints, used so that a bigger lot never makes a part slower (log lot size: -1).
Evaluated on held-out PARTS (every work order of a part is in the same split), stored in MinIO with a model card.

S8: `train` stores a CANDIDATE. The estimates keep using the current model (latest.json) until a named person promotes
the candidate, and promotion is refused unless it beats the current model on the held-out work orders (re-measured
now, on the same rows, for both). The very first model has nothing to beat and becomes current at once.

Usage (knowledge job): python -m apex_agent.costing.cycle_time train
                       python -m apex_agent.costing.cycle_time promote <version> --by <name>
"""

import io
import json
import math
import sys
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

from apex_agent.costing.history import code_of, fill_factors
from apex_agent.ingest.store import Store
from apex_agent.knowledge import db
from apex_agent.knowledge.normalize import records

QUANTILES = (0.1, 0.5, 0.9)
CATS = ("wc", "family", "material")
PREFIX = "models/cycle_time"


def held_out(part: str) -> bool:
    return zlib.crc32(part.encode()) % 5 == 0  # a stable 20 % of the parts


def training_rows(conn: Any) -> list[dict[str, Any]]:
    tmpl = {r["id"]: r for r in records(conn, "product.template")}
    prod_tmpl = {r["id"]: r["product_tmpl_id"][0] for r in records(conn, "product.product")}
    material = {}
    lines = {r["id"]: r for r in records(conn, "mrp.bom.line")}
    for b in records(conn, "mrp.bom"):
        mats = [code_of(lines[i]["product_id"][1]) for i in b["bom_line_ids"] if i in lines]
        material[b["product_tmpl_id"][0]] = next((m for m in mats if m.startswith("MAT-")), None)
    wc_code = {r["id"]: r["code"] for r in records(conn, "mrp.workcenter")}
    mos = {r["id"]: r for r in records(conn, "mrp.production") if r["state"] == "done"}
    rows = []
    for w in records(conn, "mrp.workorder"):
        mo = mos.get(w["production_id"][0])
        if not mo or not w.get("duration") or not w.get("duration_expected"):
            continue
        t = prod_tmpl.get(mo["product_id"][0])
        part = tmpl.get(t, {})
        qty = float(mo["product_qty"])
        if not part.get("x_part_family") or not material.get(t) or qty <= 0:
            continue
        rows.append({"part": part["default_code"], "wc": wc_code[w["workcenter_id"][0]], "family": part["x_part_family"],
                     "material": material[t], "qty": qty, "planned": float(w["duration_expected"]), "actual": float(w["duration"])})  # fmt: skip
    return rows


@dataclass
class Model:
    vocab: dict[str, list[str]]
    models: dict[float, HistGradientBoostingRegressor]
    card: dict[str, Any]

    def _x(self, rows: list[dict[str, Any]]) -> np.ndarray:
        def enc(field: str, v: str) -> float:
            voc = self.vocab[field]
            return float(voc.index(v)) if v in voc else float("nan")  # unseen category -> missing (handled by HGB)

        return np.array([[enc("wc", r["wc"]), enc("family", r["family"]), enc("material", r["material"]),
                          math.log(r["qty"]), math.log(max(r["planned"] / r["qty"], 0.01))] for r in rows])  # fmt: skip

    def ratios(self, rows: list[dict[str, Any]]) -> list[tuple[float, float, float]]:
        """(P10, P50, P90) of actual / planned for each row, sorted so P10 <= P50 <= P90."""
        x = self._x(rows)
        preds = np.exp(np.column_stack([self.models[q].predict(x) for q in QUANTILES]))
        k = float(self.card.get("p90_conformal_factor", 1.0))  # calibrated on held-out work orders (train())
        out = []
        for row in preds:
            p10, p50, p90 = sorted(float(v) for v in row)
            out.append((p10, p50, p90 * k))
        return out


def fit(rows: list[dict[str, Any]]) -> Model:
    vocab = {c: sorted({r[c] for r in rows}) for c in CATS}
    m = Model(vocab, {}, {})
    x = m._x(rows)
    y = np.log([r["actual"] / r["planned"] for r in rows])
    for q in QUANTILES:
        hgb = HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                            min_samples_leaf=8, categorical_features=[0, 1, 2], monotonic_cst=[0, 0, 0, -1, 1],
                                            random_state=7)  # fmt: skip
        m.models[q] = hgb.fit(x, y)
    return m


def evaluate(model: Model, test: list[dict[str, Any]]) -> dict[str, Any]:
    r = model.ratios(test)
    ape_model = [abs(p50 * t["planned"] - t["actual"]) / t["actual"] for (_, p50, _), t in zip(r, test, strict=True)]
    ape_std = [abs(t["planned"] - t["actual"]) / t["actual"] for t in test]
    inside = sum(1 for (p10, _, p90), t in zip(r, test, strict=True) if p10 * t["planned"] <= t["actual"] <= p90 * t["planned"])
    return {"work_orders": len(test), "parts": len({t["part"] for t in test}),
            "mdape_p50": round(median(ape_model), 4), "mdape_standard_times": round(median(ape_std), 4),
            "p10_p90_coverage": round(inside / len(test), 3)}  # fmt: skip


def train() -> dict[str, Any]:
    conn = db.connect()
    rows = training_rows(conn)
    train_rows = [r for r in rows if not held_out(r["part"])]
    test_rows = [r for r in rows if held_out(r["part"])]
    model = fit(train_rows)
    # Split-conformal calibration of the P90 on the held-out work orders: the factor that makes "P90" cover 90 %.
    p90 = [r[2] * t["planned"] for r, t in zip(model.ratios(test_rows), test_rows, strict=True)] if test_rows else []
    scores = sorted(t["actual"] / p for t, p in zip(test_rows, p90, strict=True))
    p90_factor = max(1.0, scores[min(len(scores) - 1, int(0.9 * (len(scores) + 1)))]) if scores else 1.0
    version = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    card = {
        "name": "apex cycle-time model", "version": version, "algorithm": "sklearn HistGradientBoostingRegressor, quantile loss",
        "quantiles": list(QUANTILES), "target": "log(actual / standard minutes) per work order",
        "features": ["work center", "part family", "material", "log lot size (monotonic, non-increasing)",
                     "log standard minutes per part (monotonic, non-decreasing)"],
        "training_data": f"{len(train_rows)} finished work orders of {len({r['part'] for r in train_rows})} parts (Odoo, S2 read model)",
        "held_out": evaluate(model, test_rows) if test_rows else {},
        "p90_conformal_factor": round(p90_factor, 3),  # applied to P90 so it covers 90 % of held-out actual times
        "limits": "Apex's history only (3 years, 8 families); a new family or material falls back to the overall pattern.",
        "fill_factor_by_family": fill_factors(conn),  # finished / envelope volume, from Apex's STEP models
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }  # fmt: skip
    model.card = card
    buf = io.BytesIO()
    joblib.dump({"vocab": model.vocab, "models": model.models}, buf)
    store = Store()
    store.put(f"{PREFIX}/{version}/model.joblib", buf.getvalue())
    store.put(f"{PREFIX}/{version}/card.json", json.dumps(card, indent=1).encode(), "application/json")
    current = _latest(store)
    if current is None:  # bootstrap: nothing to compare with yet
        _set_latest(store, {"version": version, "promoted_by": "bootstrap (first model)", "promoted_at": card["trained_at"]})
        card["status"] = "current (first model)"
    else:
        card["status"] = f"candidate: current is {current['version']}; promote with `promote {version} --by <name>`"
    return card


def _latest(store: Store) -> dict[str, Any] | None:
    try:
        return dict(json.loads(store.get(f"{PREFIX}/latest.json")))
    except Exception:  # noqa: BLE001 (MinIO's "no such key" and friends: there is no current model yet)
        return None


def _set_latest(store: Store, rec: dict[str, Any]) -> None:
    store.put(f"{PREFIX}/latest.json", json.dumps(rec).encode(), "application/json")
    history = store.get(f"{PREFIX}/promotions.jsonl") if _exists(store, f"{PREFIX}/promotions.jsonl") else b""
    store.put(f"{PREFIX}/promotions.jsonl", history + json.dumps(rec).encode() + b"\n", "application/x-ndjson")


def _exists(store: Store, key: str) -> bool:
    try:
        store.get(key)
        return True
    except Exception:  # noqa: BLE001
        return False


def promote(version: str, by: str) -> dict[str, Any]:
    """Make `version` the current model, only if it beats the current one on the held-out work orders."""
    if not by.strip():
        raise SystemExit("--by <name> is required: a person promotes a model")
    store = Store()
    current = _latest(store)
    if current and current["version"] == version:
        raise SystemExit(f"{version} is already the current model")
    test_rows = [r for r in training_rows(db.connect()) if held_out(r["part"])]
    cand = evaluate(load(store, version), test_rows)
    cur = evaluate(load(store, current["version"]), test_rows) if current else None
    if cur and cand["mdape_p50"] >= cur["mdape_p50"]:
        raise SystemExit(f"refused: candidate {version} MdAPE {cand['mdape_p50']:.4f} is not better than current "
                         f"{current['version'] if current else '-'} {cur['mdape_p50']:.4f} on {cand['work_orders']} held-out work orders")  # fmt: skip
    rec = {"version": version, "promoted_by": by.strip(), "promoted_at": datetime.now(UTC).isoformat(timespec="seconds"),
           "previous": current["version"] if current else None, "held_out": {"candidate": cand, "previous": cur}}  # fmt: skip
    _set_latest(store, rec)
    return rec


def load(store: Store | None = None, version: str | None = None) -> Model:
    """The current model from MinIO (models/cycle_time/latest.json -> its version), or a given version."""
    store = store or Store()
    version = version or json.loads(store.get(f"{PREFIX}/latest.json"))["version"]
    blob = joblib.load(io.BytesIO(store.get(f"{PREFIX}/{version}/model.joblib")))
    card = json.loads(store.get(f"{PREFIX}/{version}/card.json"))
    return Model(blob["vocab"], blob["models"], card)


if __name__ == "__main__":
    if sys.argv[1:] == ["train"]:
        print(json.dumps(train()))
    elif sys.argv[1:2] == ["promote"] and len(sys.argv) == 5 and sys.argv[3] == "--by":
        print(json.dumps(promote(sys.argv[2], sys.argv[4])))
    else:
        raise SystemExit(__doc__)
