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

__all__ = ["evaluate", "risk_metrics", "backtest_verdict", "EDGE_PERCENTILE", "N_RANDOM",
           "HORIZON", "SEED"]

N_RANDOM = 200
# The share of random re-timings the signal's Sharpe must beat before its
# timing is called better than luck.
EDGE_PERCENTILE = 0.95
HORIZON = 20
SEED = 20261116
_KEEP = ("total_return", "cagr", "annualised_volatility", "sharpe_ratio", "max_drawdown",
         "exposure", "trades", "costs_paid")


def risk_metrics(equity, held=None) -> dict:
    """Risk-adjusted figures from an equity curve, all with no risk-free rate.

    sharpe        mean daily return / its standard deviation x sqrt(252)
    sortino       mean daily return x 252 / downside deviation, where the
                  downside deviation is sqrt(mean(min(r, 0)^2)) x sqrt(252)
                  over every day (target return 0)
    calmar        CAGR / |maximum drawdown|
    time_in_market   share of days with a position (1.0 when `held` is None)
    """
    eq = np.asarray(equity, dtype=float)
    r = eq[1:] / eq[:-1] - 1.0
    years = len(r) / 252
    cagr = float((eq[-1] / eq[0]) ** (1 / years) - 1.0) if years > 0 and eq[-1] > 0 else None
    dd = float(np.min(eq / np.maximum.accumulate(eq) - 1.0))
    sd = float(np.std(r, ddof=1)) if len(r) > 1 else 0.0
    downside = float(np.sqrt(np.mean(np.minimum(r, 0.0) ** 2)) * np.sqrt(252)) if len(r) else 0.0

    def rnd(x):
        return None if x is None or not np.isfinite(x) else round(float(x), 4)

    return {
        "cagr": rnd(cagr),
        "max_drawdown": rnd(dd),
        "sharpe": rnd(np.mean(r) / sd * np.sqrt(252)) if sd > 0 else None,
        "sortino": rnd(np.mean(r) * 252 / downside) if downside > 0 else None,
        "calmar": rnd(cagr / abs(dd)) if cagr is not None and dd < 0 else None,
        "time_in_market": rnd(1.0 if held is None else
                              float(np.mean(np.abs(np.asarray(held, float)[1:]) > 0))),
    }


def _fixed(name: str, positions: np.ndarray) -> Strategy:
    return Strategy(name, name, "Positions given in advance.", {},
                    lambda p, _pos=positions: _pos)


def _stats(result) -> dict:
    return {k: result.stats.get(k) for k in _KEEP}


def _pct(values, q) -> float | None:
    v = [x for x in values if x is not None]
    return round(float(np.percentile(v, q)), 4) if v else None


def evaluate(bars, nifty_bars, market: Market, years: int = 5, n_random: int = N_RANDOM,
             regime_rule: str | None = None, refit_day: int | None = None,
             model: CostModel | None = None, wf: WalkForward | None = None,
             curves: bool = False) -> dict:
    model = model or cost_model_for(market.cost_model)
    if wf is None:
        kwargs = {}
        if regime_rule:
            kwargs["regime_rule"] = regime_rule
        if refit_day is not None:
            kwargs["refit_day"] = refit_day
        wf = walk_forward(bars, market, model, **kwargs)
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

    nifty = nifty_risk = None
    if nifty_bars:
        r = run_backtest(nifty_bars, "buy_and_hold", initial_cash=NOTIONAL, slippage_bps=0,
                         cost_model=FlatBps(0), start_day=day0)
        nifty = {k: r.benchmark_stats.get(k) for k in
                 ("total_return", "cagr", "annualised_volatility", "sharpe_ratio",
                  "max_drawdown")}
        nifty_risk = risk_metrics(r.benchmark_equity)

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
        "risk": {
            "signal": risk_metrics(signal.equity, signal.positions),
            "buy_and_hold": risk_metrics(signal.benchmark_equity),
            "nifty": nifty_risk,
        },
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
        "curves": ({"days": signal.days, "signal": signal.equity,
                    "buy_and_hold": signal.benchmark_equity} if curves else None),
        "hit_rate": {
            "horizon_days": HORIZON,
            "after_buy_up": share(buys), "buy_days": len(buys),
            "base_rate_up": share(up),
            "after_sell_down": share(sells), "sell_days": len(sells),
            "base_rate_down": share([fwd[t] < 0 for t in window]),
        },
    }


def _share(p: float) -> str:
    """A share of the random runs as a percentage, exactly: 89 of 200 is
    "44.5", not "44" (Python) or "45" (the browser), which disagreed on the
    page. One decimal is enough: the runs are 200."""
    text = f"{p * 100:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _pct_text(x: float) -> str:
    return f"{x * 100:.1f}%"


def backtest_verdict(ticker: str, ev: dict) -> dict:
    """Whether the backtest shows an edge, and a sentence that says so plainly.

    An edge needs all three, over the evaluated window: a higher CAGR than
    buy-and-hold, a higher daily Sharpe ratio, and a Sharpe that beats at least
    EDGE_PERCENTILE of the random re-timings. Anything less is "no edge".
    The sentence is built here, from the evaluation's own figures, so the
    explanation can quote it rather than paraphrase it.
    """
    if not ev.get("ok"):
        return {"verdict": "untested",
                "statement": f"The signal for {ticker} could not be backtested: "
                             f"{ev.get('error', 'not enough history')}."}
    s, b = ev["risk"]["signal"], ev["risk"]["buy_and_hold"]
    pct = ev["random"]["signal_sharpe_percentile"]
    years = ev["years"]
    edge = (s["cagr"] is not None and b["cagr"] is not None and s["cagr"] > b["cagr"]
            and s["sharpe"] is not None and b["sharpe"] is not None and s["sharpe"] > b["sharpe"]
            and pct is not None and pct >= EDGE_PERCENTILE)
    figures = (f"the signal returned {_pct_text(s['cagr'])} a year against "
               f"{_pct_text(b['cagr'])} for buy-and-hold, with a Sharpe ratio of "
               f"{s['sharpe']:.2f} against {b['sharpe']:.2f}, and beat "
               f"{_share(pct)}% of {ev['random']['runs']} random re-timings of its own trades "
               f"({EDGE_PERCENTILE * 100:.0f}% would be needed to rule out luck)")
    span = f"{years:g}-year" if years >= 1 else f"{ev['trading_days']}-day"
    if edge:
        statement = (f"The {span} backtest shows an edge for {ticker}: {figures}. Past results "
                     f"do not guarantee future ones.")
    else:
        statement = f"The {span} backtest shows no edge for {ticker}: {figures}."
    if years < 3:
        statement += f" Only {years:g} years could be tested, which is too short to judge."
    return {"verdict": "edge" if edge else "no_edge", "statement": statement,
            "window": f"{ev['start']} to {ev['end']}", "years": years,
            "signal_cagr": s["cagr"], "buy_and_hold_cagr": b["cagr"],
            "signal_sharpe": s["sharpe"], "buy_and_hold_sharpe": b["sharpe"],
            "signal_max_drawdown": s["max_drawdown"],
            "buy_and_hold_max_drawdown": b["max_drawdown"],
            "time_in_market": s["time_in_market"], "random_percentile": pct}
