"""A daily backtester: next-bar execution, transaction costs, slippage, and a
buy-and-hold comparison.

    result = run_backtest(bars, get_strategy("sma_cross"), cost_bps=5, slippage_bps=5)

Written here rather than taken from a library. vectorbt is Apache-2.0 with a
Commons Clause that forbids selling it or hosting it as a service whose value
comes mainly from it, and it brings numba, scipy, scikit-learn and pandas 3
into an API that has 512 MB; backtesting.py is AGPL-3.0, whose network clause
reaches software users interact with remotely. Checked on 2026-11-16; see
docs/BACKTESTING.md. A daily long/flat/short engine is a few dozen lines.

How a day is accounted for
--------------------------
The strategy decides at bar t's close (market/strategies.py). That decision is
executed at bar t+1's **open**, never at the close it was computed from:
trading at the same close the signal saw is the classic look-ahead error.

    overnight   close[t-1] -> open[t]    earns the position held overnight
    at the open                          trade to the new target, paying costs
    intraday    open[t] -> close[t]      earns the new position

Costs are charged on the traded notional: commission (cost_bps) plus slippage
(slippage_bps), both in basis points, on each side of every trade. A full
switch from short to long trades twice the equity. Fills recorded in the trade
list are the open moved against the trader by the slippage.

Prices are adjusted for dividends as well as splits: each bar's open, high,
low and close are scaled by adj_close / close, so a dividend is earned the
same way by the strategy and by buy-and-hold rather than looking like a fall
on its ex-date.

The whole equity is committed to the target exposure; there is no leverage
beyond 1x and no interest on cash.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from market.analytics import TRADING_DAYS
from market.strategies import Prices, Strategy, get_strategy

__all__ = ["Trade", "BacktestResult", "adjusted_prices", "run_backtest"]


@dataclass
class Trade:
    side: str                    # "long" or "short"
    entry_day: str
    entry_price: float           # the fill: open, plus slippage against the trader
    exit_day: str | None = None
    exit_price: float | None = None
    bars_held: int = 0
    net_return: float | None = None   # after commission and slippage
    status: str = "closed"       # "open" if still held at the last bar


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    days: list[str]
    equity: list[float]
    positions: list[float]
    trades: list[Trade]
    stats: dict
    benchmark_equity: list[float] = field(default_factory=list)
    benchmark_stats: dict = field(default_factory=dict)


def adjusted_prices(bars) -> Prices:
    """OHLCV arrays scaled by adj_close / close: dividend- and split-adjusted."""
    close = np.array([b.close for b in bars], dtype=float)
    factor = np.array([b.adj_close for b in bars], dtype=float) / close
    return Prices(
        open=np.array([b.open for b in bars], dtype=float) * factor,
        high=np.array([b.high for b in bars], dtype=float) * factor,
        low=np.array([b.low for b in bars], dtype=float) * factor,
        close=close * factor,
        volume=np.array([b.volume for b in bars], dtype=float),
    )


def _simulate(p: Prices, target: np.ndarray, days: list[str], initial: float,
              cost: float, slip: float):
    """Equity and position per bar, trades, and costs paid. See the module docstring."""
    n = len(p)
    equity = np.empty(n)
    held = np.zeros(n)
    equity[0] = initial
    position, trades, costs_paid, turnover = 0.0, [], 0.0, 0.0
    open_trade: Trade | None = None
    entry_bar = 0
    for t in range(1, n):
        value = equity[t - 1] * (1.0 + position * (p.open[t] / p.close[t - 1] - 1.0))
        wanted = float(target[t - 1])          # decided at yesterday's close
        if wanted != position:
            traded = abs(wanted - position)
            fee = value * traded * (cost + slip)
            value -= fee
            costs_paid += fee
            turnover += traded
            if open_trade is not None and (wanted == 0 or np.sign(wanted) != np.sign(position)):
                sign = 1.0 if open_trade.side == "long" else -1.0
                fill = p.open[t] * (1.0 - sign * slip)
                open_trade.exit_day, open_trade.exit_price = days[t], float(fill)
                open_trade.bars_held = t - entry_bar
                open_trade.net_return = float(
                    sign * (fill / open_trade.entry_price - 1.0) - 2 * cost)
                trades.append(open_trade)
                open_trade = None
            if wanted != 0 and open_trade is None:
                sign = 1.0 if wanted > 0 else -1.0
                open_trade = Trade(side="long" if sign > 0 else "short", entry_day=days[t],
                                   entry_price=float(p.open[t] * (1.0 + sign * slip)))
                entry_bar = t
            position = wanted
        equity[t] = value * (1.0 + position * (p.close[t] / p.open[t] - 1.0))
        held[t] = position
    if open_trade is not None:
        sign = 1.0 if open_trade.side == "long" else -1.0
        open_trade.exit_day, open_trade.exit_price = days[-1], float(p.close[-1])
        open_trade.bars_held = n - 1 - entry_bar
        open_trade.net_return = float(sign * (p.close[-1] / open_trade.entry_price - 1.0) - cost)
        open_trade.status = "open"
        trades.append(open_trade)
    return equity, held, trades, costs_paid, turnover


def _stats(equity: np.ndarray, held: np.ndarray, trades: list[Trade], costs: float,
           turnover: float) -> dict:
    returns = equity[1:] / equity[:-1] - 1.0
    years = len(returns) / TRADING_DAYS
    total = float(equity[-1] / equity[0] - 1.0)
    vol = float(np.std(returns, ddof=1) * math.sqrt(TRADING_DAYS)) if len(returns) > 1 else float("nan")
    cagr = float((equity[-1] / equity[0]) ** (1 / years) - 1.0) if years > 0 and equity[-1] > 0 else float("nan")
    peak = np.maximum.accumulate(equity)
    closed = [t for t in trades if t.status == "closed"]
    wins = [t for t in closed if (t.net_return or 0) > 0]

    def clean(x):
        return None if isinstance(x, float) and (math.isnan(x) or math.isinf(x)) else (
            round(x, 6) if isinstance(x, float) else x)

    return {k: clean(v) for k, v in {
        "total_return": total,
        "cagr": cagr,
        "annualised_volatility": vol,
        "sharpe_ratio": cagr / vol if vol and vol > 0 and not math.isnan(cagr) else float("nan"),
        "max_drawdown": float(np.min(equity / peak - 1.0)),
        "trades": len(trades),
        "closed_trades": len(closed),
        "win_rate": len(wins) / len(closed) if closed else float("nan"),
        "average_trade_return": float(np.mean([t.net_return for t in closed])) if closed else float("nan"),
        "exposure": float(np.mean(np.abs(held[1:]) > 0)) if len(held) > 1 else 0.0,
        "turnover": float(turnover),
        "costs_paid": float(costs),
        "final_equity": float(equity[-1]),
    }.items()}


def run_backtest(bars, strategy: Strategy | str, params: dict | None = None,
                 initial_cash: float = 10_000.0, cost_bps: float = 5.0,
                 slippage_bps: float = 5.0) -> BacktestResult:
    """Backtest one strategy on daily bars, with buy-and-hold on the same terms."""
    if len(bars) < 3:
        raise ValueError("A backtest needs at least three bars.")
    if cost_bps < 0 or slippage_bps < 0:
        raise ValueError("Costs and slippage cannot be negative.")
    strategy = get_strategy(strategy) if isinstance(strategy, str) else strategy
    prices = adjusted_prices(bars)
    days = [b.day.isoformat() for b in bars]
    used = {**strategy.params, **{k: float(v) for k, v in (params or {}).items()
                                   if k in strategy.params}}
    target = strategy.positions(prices, **used)
    cost, slip = cost_bps / 1e4, slippage_bps / 1e4

    equity, held, trades, costs, turnover = _simulate(prices, target, days, initial_cash, cost, slip)
    bench_target = get_strategy("buy_and_hold").positions(prices)
    b_equity, b_held, b_trades, b_costs, b_turnover = _simulate(
        prices, bench_target, days, initial_cash, cost, slip)
    return BacktestResult(
        strategy=strategy.key, params=used, days=days,
        equity=[round(float(x), 4) for x in equity],
        positions=[float(x) for x in held],
        trades=trades,
        stats=_stats(equity, held, trades, costs, turnover),
        benchmark_equity=[round(float(x), 4) for x in b_equity],
        benchmark_stats=_stats(b_equity, b_held, b_trades, b_costs, b_turnover),
    )
