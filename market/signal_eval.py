"""How the signal would have done, out of sample, against honest baselines.

    evaluate(bars, nifty_bars, market, years=5)

The signal engine is run walk-forward over the whole history (market/signals.py),
so every day's signal uses only the bars before it. The evaluation window is
the last `years` years, or as much as the stock has after its first signal. It
trades the signal with the same backtester and the same Indian costs as any
strategy: BUY goes long at the next open, SELL goes flat, HOLD keeps the
position.

Compared with:

- **buy and hold** of the same stock over the same days, same costs;
- **the Nifty 50** over the same days (index level, no costs: an index is
  not traded directly);
- **random timing**: the signal's own position series, circularly shifted by
  a random offset, 200 times. A shift keeps exactly the same exposure and
  (but for the wrap-around) the same number of trades, and changes only *when*
  they happen. Beating coin flips would be easy for anything that is mostly
  invested in a rising market; beating its own shifted copies means the
  timing itself carried information.
- **hit rate**: after a BUY day, how often the stock was higher 20 trading days
  later, against how often it was higher after any day in the window (the
  base rate). And the same for SELL and lower.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from market.backtest import adjusted_prices, run_backtest
from market.costs import CostModel, FlatBps, cost_model_for
from market.markets import Market
from market.signals import NOTIONAL, SLIPPAGE_BPS, WalkForward, walk_forward
from market.strategies import Strategy

__all__ = ["evaluate", "N_RANDOM", "HORIZON", "SEED"]

N_RANDOM = 200
HORIZON = 20
SEED = 20261116
_KEEP = ("total_return", "cagr", "annualised_volatility", "sharpe_ratio", "max_drawdown",
         "exposure", "trades", "costs_paid")


def _fixed(name: str, positions: np.ndarray) -> Strategy:
    return Strategy(name, name, "Positions given in advance.", {},
                    lambda p, _pos=positions: _pos)


def _stats(result) -> dict:
    return {k: result.stats.get(k) for k in _KEEP}


def _pct(values, q) -> float | None:
    v = [x for x in values if x is not None]
    return round(float(np.percentile(v, q)), 4) if v else None


def evaluate(bars, nifty_bars, market: Market, years: int = 5, n_random: int = N_RANDOM,
             model: CostModel | None = None, wf: WalkForward | None = None) -> dict:
    model = model or cost_model_for(market.cost_model)
    wf = wf or walk_forward(bars, market, model)
    n = len(bars)
    wanted = bars[-1].day - timedelta(days=round(365.25 * years))
    start = max(wf.first, next(i for i, b in enumerate(bars) if b.day >= wanted))
    if n - start < 60:
        return {"ok": False, "error": f"only {n - start} bars after the first signal"}
    day0 = bars[start].day
    run = dict(initial_cash=NOTIONAL, slippage_bps=SLIPPAGE_BPS, cost_model=model,
               start_day=day0)

    signal = run_backtest(bars, _fixed("signal", wf.position), **run)
    oos = wf.position[start:]
    rng = np.random.default_rng(SEED)
    offsets = rng.choice(np.arange(1, len(oos)), size=min(n_random, len(oos) - 1), replace=False)
    random = []
    for k in offsets:
        shifted = np.concatenate([np.zeros(start), np.roll(oos, int(k))])
        random.append(_stats(run_backtest(bars, _fixed("random", shifted), **run)))

    nifty = None
    if nifty_bars:
        r = run_backtest(nifty_bars, "buy_and_hold", initial_cash=NOTIONAL, slippage_bps=0,
                         cost_model=FlatBps(0), start_day=day0)
        nifty = {k: r.benchmark_stats.get(k) for k in
                 ("total_return", "cagr", "annualised_volatility", "sharpe_ratio",
                  "max_drawdown")}

    close = adjusted_prices(bars).close
    fwd = np.full(n, np.nan)
    fwd[:n - HORIZON] = close[HORIZON:] / close[:n - HORIZON] - 1.0
    window = range(start, n - HORIZON)
    up = [fwd[t] > 0 for t in window]
    buys = [fwd[t] > 0 for t in window if wf.decision[t] == "BUY"]
    sells = [fwd[t] < 0 for t in window if wf.decision[t] == "SELL"]

    def share(xs):
        return round(float(np.mean(xs)), 4) if xs else None

    sharpe = signal.stats.get("sharpe_ratio")
    random_sharpes = [r["sharpe_ratio"] for r in random if r["sharpe_ratio"] is not None]
    decisions = [wf.decision[t] for t in range(start, n)]
    regimes = [wf.regime[t] for t in range(start, n)]
    limited_days = sum(1 for t in range(start, n) if wf.fits[t - wf.first].limited)
    return {
        "ok": True,
        "start": day0.isoformat(), "end": bars[-1].day.isoformat(),
        "trading_days": n - start, "years": round((n - start) / 252, 2),
        "years_requested": years,
        "limited_history_days": limited_days,
        "decisions": {d: decisions.count(d) for d in ("BUY", "SELL", "HOLD")},
        "regimes": {r: regimes.count(r) for r in sorted(set(regimes))},
        "signal": _stats(signal),
        "buy_and_hold": {k: signal.benchmark_stats.get(k) for k in _KEEP},
        "nifty": nifty,
        "random": {
            "runs": len(random),
            "cagr_p5": _pct([r["cagr"] for r in random], 5),
            "cagr_median": _pct([r["cagr"] for r in random], 50),
            "cagr_p95": _pct([r["cagr"] for r in random], 95),
            "sharpe_p5": _pct(random_sharpes, 5),
            "sharpe_median": _pct(random_sharpes, 50),
            "sharpe_p95": _pct(random_sharpes, 95),
            "signal_sharpe_percentile": (
                round(float(np.mean([s < sharpe for s in random_sharpes])), 4)
                if sharpe is not None and random_sharpes else None),
        },
        "hit_rate": {
            "horizon_days": HORIZON,
            "after_buy_up": share(buys), "buy_days": len(buys),
            "base_rate_up": share(up),
            "after_sell_down": share(sells), "sell_days": len(sells),
            "base_rate_down": share([fwd[t] < 0 for t in window]),
        },
    }
