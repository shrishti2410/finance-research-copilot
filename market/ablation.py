"""Quant honesty: the signal against simpler ensembles and an exposure-matched
benchmark, with full metrics and the rules' correlation.

Research code for benchmarks/QUANT_HONESTY.md, run by scripts/quant_honesty.py.
The product does not import it. What is measured, and the adoption rules,
were written before the first run: benchmarks/QUANT_HONESTY_RULES.md.

Every ensemble here is walk-forward on the production signal's own monthly
refits (`WalkForward.fits`): on each day it sees only the rules that carried a
weight at the latest refit, and those weights were fitted on bars before it.
Positions are decided at the close and traded at the next open with the same
backtester and costs as the signal (`market/signal_eval.py`).

    ENSEMBLES             how a day's position is decided from the eligible rules
    ensemble_positions()  the position series of one ensemble
    exposure_matched()    buy-and-hold scaled to a fixed exposure, the rest in cash
    full_metrics()        CAGR, max drawdown, Sharpe, Sortino, exposure,
                          turnover, cost drag
    rule_returns()        each rule's daily net returns (for correlation and the
                          hindsight best rule)
"""

from __future__ import annotations

import numpy as np

from market.costs import CostModel, FlatBps
from market.markets import Market
from market.signal_eval import _fixed, risk_metrics
from market.signals import (
    BUY_AT,
    FIT,
    NOTIONAL,
    SELL_AT,
    SLIPPAGE_BPS,
    STYLE,
    WalkForward,
    _vote,
)
from market.backtest import run_backtest
from market.strategies import STRATEGIES

# "regime" is the production rule: Sharpe weights times the regime multipliers.
ENSEMBLES = ("regime", "performance", "equal", "majority", "best_rule")


def _threshold(score: float, held: float) -> float:
    return 1.0 if score >= BUY_AT else 0.0 if score <= SELL_AT else held


def ensemble_positions(wf: WalkForward, market: Market, kind: str) -> np.ndarray:
    """0/1 position per bar (0 before the first signal) for one ensemble.

    regime       sum(w x regime multiplier x vote) / sum(w x multiplier), w = trailing
                 Sharpe floored at 0; BUY at +0.25, SELL at -0.25, HOLD keeps. With
                 drift down-weighting off, this is the production signal.
    performance  the same without the regime multipliers
    equal        every eligible rule weighs 1; same thresholds
    majority     in when more than half of the eligible rules are in, else out
    best_rule    the eligible rule with the highest trailing Sharpe at the refit;
                 its own position (long or flat) until the next refit
    """
    if kind not in ENSEMBLES:
        raise ValueError(f"unknown ensemble {kind!r}; one of {ENSEMBLES}")
    n = len(wf.days)
    pos = np.zeros(n)
    held = 0.0
    for t in range(wf.first, n):
        fit = wf.fits[t - wf.first]
        eligible = list(fit.weights)
        if not eligible:
            pos[t] = held
            continue
        if kind == "majority":
            ins = sum(1 for k in eligible if wf.targets[k][t] > 0)
            held = 1.0 if ins > len(eligible) / 2 else 0.0
        elif kind == "best_rule":
            best = max(eligible, key=lambda k: (fit.weights[k]["train_sharpe"], k))
            held = 1.0 if wf.targets[best][t] > 0 else 0.0
        else:
            mult = FIT.get(wf.regime[t], {}) if kind == "regime" else {}
            num = den = 0.0
            for k in eligible:
                w = 1.0 if kind == "equal" else fit.weights[k]["weight"]
                w *= mult.get(STYLE[k], 1.0)
                num += w * _vote(wf.targets[k][t], k, market)
                den += w
            held = _threshold(num / den if den > 0 else 0.0, held)
        pos[t] = held
    return pos


def exposure_matched(benchmark_equity, exposure: float) -> list[float]:
    """Buy-and-hold scaled to a fixed exposure, the rest in cash at 0%: each
    day's return is `exposure` x the benchmark's. Same start value."""
    eq = np.asarray(benchmark_equity, dtype=float)
    r = eq[1:] / eq[:-1] - 1.0
    out = np.empty_like(eq)
    out[0] = eq[0]
    out[1:] = eq[0] * np.cumprod(1.0 + exposure * r)
    return out.tolist()


def turnover_per_year(positions) -> float:
    """Position changes (in or out) per year of the series."""
    p = np.asarray(positions, dtype=float)
    if len(p) < 2:
        return 0.0
    return float(np.sum(np.abs(np.diff(p)) > 0) / (len(p) / 252))


def _run(bars, positions, day0, model: CostModel, gross: bool = False):
    return run_backtest(bars, _fixed("ablation", positions), initial_cash=NOTIONAL,
                        slippage_bps=0.0 if gross else SLIPPAGE_BPS,
                        cost_model=FlatBps(0) if gross else model, start_day=day0)


def full_metrics(bars, positions, day0, model: CostModel) -> dict:
    """The pre-registered metrics for one position series over the window from
    `day0`, net of costs; cost drag = gross CAGR - net CAGR."""
    net = _run(bars, positions, day0, model)
    gross = _run(bars, positions, day0, model, gross=True)
    m = risk_metrics(net.equity, net.positions)
    g = risk_metrics(gross.equity, gross.positions)
    held = np.asarray(net.positions, dtype=float)
    return {
        "cagr": m["cagr"], "max_drawdown": m["max_drawdown"], "sharpe": m["sharpe"],
        "sortino": m["sortino"], "exposure": m["time_in_market"],
        "turnover_per_year": round(turnover_per_year(held), 2),
        "cost_drag": (None if m["cagr"] is None or g["cagr"] is None
                      else round(g["cagr"] - m["cagr"], 4)),
        "_equity": net.equity, "_benchmark_equity": net.benchmark_equity,
    }


def rule_returns(bars, wf: WalkForward, start: int, model: CostModel) -> dict[str, np.ndarray]:
    """Each candidate rule's daily net returns from bar `start`, traded alone
    with the signal's costs (the same series the walk-forward weights use)."""
    out = {}
    for key in wf.candidates:
        eq = np.asarray(run_backtest(bars, STRATEGIES[key], initial_cash=NOTIONAL,
                                     slippage_bps=SLIPPAGE_BPS, cost_model=model).equity)
        r = np.zeros(len(eq))
        r[1:] = eq[1:] / eq[:-1] - 1.0
        out[key] = r[start + 1:]
    return out


def correlation(returns: dict[str, np.ndarray]) -> tuple[list[str], np.ndarray]:
    """Pairwise correlation of the rules' daily returns. A rule whose returns
    never vary (never in the stock) has NaN rows."""
    keys = sorted(returns)
    m = np.vstack([returns[k] for k in keys])
    with np.errstate(invalid="ignore", divide="ignore"):
        c = np.corrcoef(m)
    return keys, c
