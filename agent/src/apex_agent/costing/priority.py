"""Queue priority (S11, W1: the inbox was worked oldest-first). Deterministic, every factor shown on the screen:

    score = P(win) x expected value x capacity fit x urgency

  P(win)        the win rate of the analog parts Apex quoted before (parts.won / parts.quotes), blended with the baseline
                so a few quotes don't give 0 % or 100 %; S14 replaces it with
                the calibrated win model. No analogs: the L6 baseline win rate (26.2 %).
  value         the quote value (first quantity break) in USD
  capacity fit  1.0 if the requested date can be met, 0.5 if not (the shop is booked)
  urgency       1.5 if the customer needs it within 14 days, 1.2 within 30, else 1.0
A no-bid has no value: it sinks to the bottom (its decline still waits for a person, oldest first among them).
"""

from datetime import date
from typing import Any

BASELINE_WIN_RATE = 0.262  # docs/legacy/BASELINE_METRICS.md (L6)
PRIOR_QUOTES = 4  # how strongly the baseline counts against a few analog quotes (a Beta prior)


def score(conn: Any, estimates: list[dict[str, Any]], value: float, requirements: dict[str, Any], infeasible: bool,
          no_bid: bool) -> dict[str, Any]:  # fmt: skip
    codes = sorted({a for e in estimates for a in (e.get("analogs") or {}).get("analogs") or []})
    p_win, basis = BASELINE_WIN_RATE, "the L6 baseline win rate (no analogs)"
    wins = [e["win"] for e in estimates if (e.get("win") or {}).get("validated")]
    if wins:  # S14: the win model, once it beat the baseline (a NOT validated model is never used here)
        p_win = round(sum(w["p_win"] for w in wins) / len(wins), 3)
        basis = (
            f"win model {wins[0]['model']} (Brier {wins[0]['brier']['model']} vs {wins[0]['brier']['baseline_average_win_rate']})"
        )
    elif codes:
        with conn.cursor() as cur:
            cur.execute("SELECT coalesce(sum(won), 0), coalesce(sum(quotes), 0) FROM parts WHERE code = ANY(%s)", (codes,))
            won, quoted = cur.fetchone()
        if quoted:  # blended with the baseline (as if 4 more quotes at 26.2 %): 0 wins of 2 quotes is not "0 % chance"
            p_win = round((float(won) + PRIOR_QUOTES * BASELINE_WIN_RATE) / (float(quoted) + PRIOR_QUOTES), 3)
            basis = f"{won} won of {quoted} quotes of {len(codes)} analog parts, blended with the baseline"
    requested = (requirements.get("requested_date") or {}).get("value")
    days = (date.fromisoformat(str(requested)) - date.today()).days if requested else None
    urgency = 1.5 if days is not None and days <= 14 else 1.2 if days is not None and days <= 30 else 1.0
    fit = 0.5 if infeasible else 1.0
    s = 0.0 if no_bid else round(p_win * value * fit * urgency, 2)
    return {"score": s, "p_win": p_win, "p_win_basis": basis, "value_usd": round(value, 2), "capacity_fit": fit,
            "urgency": urgency, "days_to_requested": days,
            "explain": "no-bid: a decline to approve" if no_bid else
            f"{p_win:.0%} win x ${value:,.0f} x fit {fit} x urgency {urgency} = {s:,.0f}"}  # fmt: skip
