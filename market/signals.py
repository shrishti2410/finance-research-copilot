"""BUY / SELL / HOLD from a vote of the strategies, weighted walk-forward.

Every number here is computed in Python from the price cache. Nothing is
predicted, and no language model reads a price: the agent only explains the
evidence this module returns, citing the strategy cards.

How a day's signal is made (all from bars 0..t, decided at bar t's close)
-------------------------------------------------------------------------
1. **Regime** (market/regime.py): trending up or down, ranging, transition,
   volatile, from ADX, the 50-day average's slope and volatility percentile.
2. **Votes.** Each strategy says long (+1) or out (-1) at t. Every strategy
   here is long or flat on an Indian stock, so "out" is a vote to sell or stay
   away.
3. **Weights, walk-forward.** On the first trading day of each month, each
   strategy's weight is reset to its Sharpe ratio over the previous
   TRAIN_DAYS bars, net of the market's costs, floored at zero. Those weights
   are then used, unchanged, for the rest of the month, which they have not
   seen. The rules'
   parameters are the textbook defaults and are never fitted, so the only
   thing learned from the past is how much to trust each rule.
4. **Regime fit.** A strategy whose style suits the regime keeps its weight;
   one that does not is halved (trend rules in a range, reversion rules in a
   trend). Fixed beforehand, not fitted.
5. **Score** = weighted mean vote, from -1 (every trusted rule out) to +1
   (every trusted rule in). BUY at >= +0.25, SELL at <= -0.25, HOLD between.
6. **Coverage and confidence.** Coverage is how many of the rules eligible
   at this refit carry any weight, e.g. "3 of 11 rules". **Confidence** =
   |score| x history factor (below), capped at the coverage fraction: a
   score that three rules out of eleven agree on cannot read as more than
   0.27, however one-sided it is. Fewer than half the rules counting adds a
   warning. Confidence is for display only; the decision in step 5 does not
   use it. (Added 2026-11-16 after the first evaluation, which showed
   PREMIERENE.NS at +0.89 on three rules; it changes no signal.)

Limited history
---------------
At a refit on bar t, a stock with fewer than FULL_HISTORY_TRADING_DAYS bars up
to t (market/universe.py) trains on SHORT_TRAIN_DAYS instead of TRAIN_DAYS. It
skips every strategy whose warm-up is more than a third of those bars: a
200-day rule on 546 days would be fitted on almost nothing. Its confidence is
scaled by train / TRAIN_DAYS, and the result carries a warning.

All of that is decided from the bars up to t, never from how many bars the
series has in total. Otherwise the set of voters on a past day would depend
on how much data came after it, which is look-ahead.

Why a calendar day for the refit
--------------------------------
Until 2026-11-17 the weights were refitted every 21 bars counted from the
first signal, so where the history happened to start decided which days were
refit days. Fetching one day less history moved every refit, and moved the
five-year results (COFORGE.NS: CAGR 3.5% on one start, 4.4% on the next). A
result that depends on an arbitrary start is noise, and a live signal that
changes because the fetch window rolled forward a day is worse. Refitting on
the first trading day of each month ties the schedule to the calendar. The
`refit_day` argument (the k-th trading day of the month) exists to measure
how much that choice matters (benchmarks/SIGNALS_7C.md).

The thresholds, the regime multipliers and the window lengths were set before
the evaluation was run, and were not changed after it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np

from market.backtest import adjusted_prices, run_backtest
from market.costs import CostModel, cost_model_for
from market.markets import Market
from market.regime import DEFAULT_RULE, classify, measure
from market.strategies import STRATEGIES
from market.universe import FULL_HISTORY_TRADING_DAYS

__all__ = ["STYLE", "WARMUP", "FIT", "TRAIN_DAYS", "SHORT_TRAIN_DAYS", "REFIT_EVERY",
           "BUY_AT", "SELL_AT", "Fit", "WalkForward", "walk_forward", "signal_evidence",
           "DISCLAIMER"]

DISCLAIMER = "Educational project, not investment advice."

# Trend rules want a trend; reversion rules want a range.
STYLE = {
    "sma_cross": "trend", "ema_cross": "trend", "price_above_sma": "trend",
    "macd_cross": "trend", "bollinger_breakout": "trend", "donchian_breakout": "trend",
    "momentum": "trend", "atr_trailing_stop": "trend", "sma_cross_long_short": "trend",
    "rsi2_pullback": "trend", "keltner_breakout": "trend", "adx_trend": "trend",
    "trend_alignment": "trend", "obv_trend": "trend",
    "rsi_reversion": "reversion", "bollinger_reversion": "reversion",
    "zscore_reversion": "reversion",
}
# Bars before a strategy can first hold a position, at its default parameters.
WARMUP = {
    "sma_cross": 200, "ema_cross": 26, "price_above_sma": 200, "macd_cross": 34,
    "rsi_reversion": 15, "bollinger_reversion": 20, "bollinger_breakout": 20,
    "donchian_breakout": 20, "momentum": 252, "zscore_reversion": 20,
    "atr_trailing_stop": 50, "sma_cross_long_short": 200, "rsi2_pullback": 200,
    "keltner_breakout": 20, "adx_trend": 27, "trend_alignment": 200, "obv_trend": 50,
}
FIT = {  # regime -> style -> multiplier; any other regime leaves weights as they are
    "trending_up": {"trend": 1.0, "reversion": 0.5},
    "trending_down": {"trend": 1.0, "reversion": 0.5},
    "ranging": {"trend": 0.5, "reversion": 1.0},
}
TRAIN_DAYS = 504          # two years
SHORT_TRAIN_DAYS = 126    # six months, for a stock with limited history
REFIT_EVERY = "monthly"   # on the REFIT_DAY-th trading day of each month
REFIT_DAY = 0             # 0: the first trading day
BUY_AT, SELL_AT = 0.25, -0.25
MIN_COVERAGE = 0.5        # below this share of rules counting, the vote is "narrow"
SLIPPAGE_BPS = 5.0
NOTIONAL = 100_000.0      # the account the per-trade charges are computed on


@dataclass
class Fit:
    """One refit: the weights, and what was left out and why."""
    on: date
    bars: int
    limited: bool
    train_days: int
    weights: dict[str, dict]
    skipped: dict[str, str]

    @property
    def history_factor(self) -> float:
        return round(min(1.0, self.train_days / TRAIN_DAYS), 4)


@dataclass
class WalkForward:
    days: list[date]
    candidates: list[str]
    excluded: dict[str, str]         # never voters on this market
    first: int                       # first bar with a signal
    score: np.ndarray                # NaN before `first`
    decision: list[str | None]
    position: np.ndarray             # 1 in, 0 out; HOLD keeps yesterday's
    targets: dict[str, np.ndarray]
    fits: list[Fit]                  # per bar from `first`: the fit in force
    regime: list[str]
    measures: dict[str, np.ndarray]


def _sharpe(r: np.ndarray) -> float:
    sd = float(np.std(r, ddof=1)) if len(r) > 1 else 0.0
    return 0.0 if sd == 0 or math.isnan(sd) else float(np.mean(r) / sd * math.sqrt(252))


def _candidates(market: Market) -> tuple[list[str], dict[str, str]]:
    keys, excluded = [], {}
    for key in STRATEGIES:
        if key == "buy_and_hold":
            continue                                     # the benchmark, not a view
        if key == "sma_cross_long_short" and not market.allows_overnight_short:
            excluded[key] = "no overnight shorts on this market: identical to sma_cross"
            continue
        keys.append(key)
    return keys, excluded


def _refit(t: int, day: date, candidates: list[str], returns: dict[str, np.ndarray]) -> Fit:
    bars = t + 1
    limited = bars < FULL_HISTORY_TRADING_DAYS
    train = SHORT_TRAIN_DAYS if limited else TRAIN_DAYS
    weights, skipped = {}, {}
    for key in candidates:
        if limited and WARMUP[key] > bars / 3:
            skipped[key] = (f"limited history: needs {WARMUP[key]} bars of warm-up, more "
                            f"than a third of the {bars} bars available")
        elif WARMUP[key] + train > bars:
            skipped[key] = f"warming up: needs {WARMUP[key] + train} bars, has {bars}"
        else:
            s = _sharpe(returns[key][t - train + 1:t + 1])
            weights[key] = {"train_sharpe": round(s, 4), "weight": max(s, 0.0)}
    return Fit(on=day, bars=bars, limited=limited, train_days=train,
               weights=weights, skipped=skipped)


def _vote(position: float, allows_short: bool) -> float:
    return float(position) if allows_short else 2.0 * float(position) - 1.0


def _day_in_month(bars) -> list[int]:
    """For each bar, how many earlier bars of the same month there are."""
    out = []
    for i, b in enumerate(bars):
        same = i > 0 and (b.day.year, b.day.month) == (bars[i - 1].day.year, bars[i - 1].day.month)
        out.append(out[-1] + 1 if same else 0)
    return out


def walk_forward(bars, market: Market, model: CostModel | None = None,
                 regime_rule: str = DEFAULT_RULE, refit_day: int = REFIT_DAY) -> WalkForward:
    model = model or cost_model_for(market.cost_model)
    n = len(bars)
    candidates, excluded = _candidates(market)
    prices = adjusted_prices(bars)
    shorting = market.allows_overnight_short

    targets: dict[str, np.ndarray] = {}
    returns: dict[str, np.ndarray] = {}
    for key in candidates:
        st = STRATEGIES[key]
        pos = st.positions(prices)
        targets[key] = pos if shorting else np.clip(pos, 0.0, None)
        # Net daily returns, next-bar execution, the market's own costs. The
        # equity at bar t uses positions decided up to t-1 and prices up to t.
        eq = np.asarray(run_backtest(bars, st, initial_cash=NOTIONAL,
                                     slippage_bps=SLIPPAGE_BPS, cost_model=model).equity)
        r = np.zeros(n)
        r[1:] = eq[1:] / eq[:-1] - 1.0
        returns[key] = r

    m = measure(prices.high, prices.low, prices.close)
    regime = classify(m, regime_rule)
    # The first bar at which the quickest rule has warmed up and trained.
    first = min(WARMUP[k] for k in candidates) + SHORT_TRAIN_DAYS - 1
    score = np.full(n, np.nan)
    decision: list[str | None] = [None] * n
    position = np.zeros(n)
    fits: list[Fit] = []
    fit: Fit | None = None
    held = 0.0
    in_month = _day_in_month(bars)
    for t in range(first, n):
        # The first signal needs weights; after that, refit on the calendar
        # day. A month with fewer trading days than refit_day + 1 keeps the
        # previous month's weights.
        if fit is None or in_month[t] == refit_day:
            fit = _refit(t, bars[t].day, candidates, returns)
        mult = FIT.get(regime[t], {})
        num = den = 0.0
        for key, f in fit.weights.items():
            w = f["weight"] * mult.get(STYLE[key], 1.0)
            num += w * _vote(targets[key][t], shorting)
            den += w
        score[t] = num / den if den > 0 else 0.0
        decision[t] = "BUY" if score[t] >= BUY_AT else "SELL" if score[t] <= SELL_AT else "HOLD"
        held = 1.0 if decision[t] == "BUY" else 0.0 if decision[t] == "SELL" else held
        position[t] = held
        fits.append(fit)
    return WalkForward(days=[b.day for b in bars], candidates=candidates, excluded=excluded,
                       first=first, score=score, decision=decision, position=position,
                       targets=targets, fits=fits, regime=regime, measures=m)


def _num(x, digits=4):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), digits)


def signal_evidence(wf: WalkForward, ticker: str, market: Market, t: int | None = None) -> dict:
    """The signal at bar t (the last bar by default) and everything behind it."""
    t = len(wf.days) - 1 if t is None else t
    if t < wf.first:
        return {"ok": False, "ticker": ticker, "disclaimer": DISCLAIMER,
                "error": f"Not enough history for a signal: {t + 1} trading days, "
                         f"the first signal needs {wf.first + 1}."}
    fit = wf.fits[t - wf.first]
    regime = wf.regime[t]
    mult = FIT.get(regime, {})
    effective = {k: f["weight"] * mult.get(STYLE[k], 1.0) for k, f in fit.weights.items()}
    total = sum(effective.values())
    shorting = market.allows_overnight_short
    votes = []
    for key, f in fit.weights.items():
        pos = float(wf.targets[key][t])
        votes.append({
            "strategy": key, "name": STRATEGIES[key].name, "style": STYLE[key],
            "position": "in" if pos > 0 else ("short" if pos < 0 else "out"),
            "vote": _vote(pos, shorting),
            "train_sharpe": f["train_sharpe"],
            "weight": _num(f["weight"]),
            "regime_multiplier": mult.get(STYLE[key], 1.0),
            "share_of_vote": _num(effective[key] / total) if total > 0 else 0.0,
            "card": f"docs/strategies/{key}.md",
        })
    votes.sort(key=lambda v: (-(v["share_of_vote"] or 0), v["strategy"]))
    score = float(wf.score[t])
    counted = sum(1 for f in fit.weights.values() if f["weight"] > 0)
    eligible = len(fit.weights)
    coverage = counted / eligible if eligible else 0.0
    raw_confidence = abs(score) * fit.history_factor
    warnings = []
    if eligible and coverage < MIN_COVERAGE:
        warnings.append(f"Narrow vote: only {counted} of {eligible} rules had a positive "
                        f"Sharpe ratio in training, so the score rests on few rules; "
                        f"confidence capped at {round(coverage, 4)}.")
    if fit.limited:
        warnings.append(f"Limited history: {t + 1} trading days since "
                        f"{wf.days[0].isoformat()}. Trained on {fit.train_days} days instead "
                        f"of {TRAIN_DAYS}, {len(fit.skipped)} slower strategies left out; "
                        f"confidence scaled by {fit.history_factor}.")
    if total == 0:
        warnings.append("No strategy had a positive Sharpe ratio in its training window, "
                        "so no rule is trusted and the signal is HOLD.")
    m = wf.measures
    return {
        "ok": True, "ticker": ticker, "as_of": wf.days[t].isoformat(),
        "exchange": market.exchange, "currency": market.currency,
        "signal": wf.decision[t], "score": round(score, 4),
        "confidence": round(min(raw_confidence, coverage), 4),
        "confidence_uncapped": round(raw_confidence, 4),
        "coverage": {"rules_counted": counted, "rules_eligible": eligible,
                     "fraction": round(coverage, 4),
                     "text": f"{counted} of {eligible} rules"},
        "thresholds": {"buy_at": BUY_AT, "sell_at": SELL_AT},
        "regime": {
            "label": regime, "adx": _num(m["adx"][t], 2), "plus_di": _num(m["plus_di"][t], 2),
            "minus_di": _num(m["minus_di"][t], 2),
            "sma50_slope_20d": _num(m["sma50_slope_20d"][t]),
            "volatility": _num(m["volatility"][t]),
            "volatility_percentile": _num(m["volatility_percentile"][t]),
        },
        "votes": votes,
        "voters_in": sum(1 for v in votes if v["position"] == "in"),
        "voters_out": sum(1 for v in votes if v["position"] != "in"),
        "skipped_strategies": {**wf.excluded, **fit.skipped},
        "history": {"trading_days": t + 1, "first_day": wf.days[0].isoformat(),
                    "limited": fit.limited, "train_days": fit.train_days,
                    "history_factor": fit.history_factor},
        "method": {"train_days": fit.train_days, "refit_every": REFIT_EVERY,
                   "weights_fitted_on": fit.on.isoformat(),
                   "weight": "max(0, Sharpe of the strategy's net daily returns over the "
                             "training window)",
                   "decided": "at the close of as_of; a trade would fill at the next open"},
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }
