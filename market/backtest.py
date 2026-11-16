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
from market.costs import CostModel, FlatBps
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
              model: CostModel, slip: float):
    """Equity and position per bar, trades, and costs paid. See the module docstring.

    Each change of position is priced as a sell leg (exposure reduced) and a
    buy leg (exposure added), each through the cost model; slippage is charged
    on the whole traded notional on top.
    """
    n = len(p)
    equity = np.empty(n)
    held = np.zeros(n)
    equity[0] = initial
    position, trades, costs_paid, turnover = 0.0, [], 0.0, 0.0
    open_trade: Trade | None = None
    entry_bar, entry_fees, entry_notional = 0, 0.0, 0.0
    for t in range(1, n):
        value = equity[t - 1] * (1.0 + position * (p.open[t] / p.close[t - 1] - 1.0))
        wanted = float(target[t - 1])          # decided at yesterday's close
        if wanted != position:
            bought = max(0.0, wanted - position)
            sold = max(0.0, position - wanted)
            # A long being reduced is a sale; a short being reduced is a
            # purchase. Signs only matter to the market's side-specific
            # charges (stamp duty on buys, the DP charge on sales).
            sell_notional = value * (min(sold, max(position, 0.0)) + min(bought, max(-position, 0.0)))
            buy_notional = value * (bought + sold) - sell_notional
            sale = model.charges("sell", sell_notional).total if sell_notional else 0.0
            purchase = model.charges("buy", buy_notional).total if buy_notional else 0.0
            fee = sale + purchase + value * (bought + sold) * slip
            closing = open_trade is not None and (wanted == 0 or np.sign(wanted) != np.sign(position))
            if closing:
                sign = 1.0 if open_trade.side == "long" else -1.0
                fill = p.open[t] * (1.0 - sign * slip)
                open_trade.exit_day, open_trade.exit_price = days[t], float(fill)
                open_trade.bars_held = t - entry_bar
                exit_fees = sale if sign > 0 else purchase
                open_trade.net_return = float(
                    sign * (fill / open_trade.entry_price - 1.0)
                    - (entry_fees + exit_fees) / entry_notional)
                trades.append(open_trade)
                open_trade = None
            value -= fee
            costs_paid += fee
            turnover += bought + sold
            if wanted != 0 and open_trade is None:
                sign = 1.0 if wanted > 0 else -1.0
                open_trade = Trade(side="long" if sign > 0 else "short", entry_day=days[t],
                                   entry_price=float(p.open[t] * (1.0 + sign * slip)))
                entry_bar = t
                entry_notional = value * abs(wanted)
                entry_fees = (purchase if sign > 0 else sale) if not closing else (
                    model.charges("buy" if sign > 0 else "sell", entry_notional).total)
                # A trade's return is on the cash committed to it, fees included.
                entry_notional += entry_fees
            position = wanted
        equity[t] = value * (1.0 + position * (p.close[t] / p.open[t] - 1.0))
        held[t] = position
    if open_trade is not None:
        sign = 1.0 if open_trade.side == "long" else -1.0
        open_trade.exit_day, open_trade.exit_price = days[-1], float(p.close[-1])
        open_trade.bars_held = n - 1 - entry_bar
        open_trade.net_return = float(sign * (p.close[-1] / open_trade.entry_price - 1.0)
                                      - entry_fees / entry_notional)
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


def _from(prices: Prices, i: int) -> Prices:
    return Prices(prices.open[i:], prices.high[i:], prices.low[i:], prices.close[i:],
                  prices.volume[i:])


def run_backtest(bars, strategy: Strategy | str, params: dict | None = None,
                 initial_cash: float = 10_000.0, cost_bps: float = 5.0,
                 slippage_bps: float = 5.0, start_day=None,
                 cost_model: CostModel | None = None) -> BacktestResult:
    """Backtest one strategy on daily bars, with buy-and-hold on the same terms.

    `start_day`: trade from the first bar on or after it; the bars before it are
    warm-up, read by the strategy (a 200-day average needs 200 days) but not
    traded. Without it a slow strategy sits flat for its first year while
    buy-and-hold is invested, and the comparison measures the warm-up.

    `cost_model`: how each leg is charged (market/costs.py); `cost_bps` flat
    when omitted. A model that does not allow shorts (Indian delivery) turns a
    short position into flat.
    """
    if cost_bps < 0 or slippage_bps < 0:
        raise ValueError("Costs and slippage cannot be negative.")
    strategy = get_strategy(strategy) if isinstance(strategy, str) else strategy
    all_prices = adjusted_prices(bars)
    used = {**strategy.params, **{k: float(v) for k, v in (params or {}).items()
                                   if k in strategy.params}}
    all_target = strategy.positions(all_prices, **used)
    first = 0 if start_day is None else next(
        (i for i, b in enumerate(bars) if b.day >= start_day), len(bars))
    if len(bars) - first < 3:
        raise ValueError("A backtest needs at least three bars to trade.")
    prices, target = _from(all_prices, first), all_target[first:]
    days = [b.day.isoformat() for b in bars[first:]]
    model = cost_model or FlatBps(cost_bps)
    slip = slippage_bps / 1e4
    if not model.allows_short:
        target = np.clip(target, 0.0, None)

    equity, held, trades, costs, turnover = _simulate(prices, target, days, initial_cash, model, slip)
    bench_target = np.ones(len(prices))
    b_equity, b_held, b_trades, b_costs, b_turnover = _simulate(
        prices, bench_target, days, initial_cash, model, slip)
    return BacktestResult(
        strategy=strategy.key, params=used, days=days,
        equity=[round(float(x), 4) for x in equity],
        positions=[float(x) for x in held],
        trades=trades,
        stats=_stats(equity, held, trades, costs, turnover),
        benchmark_equity=[round(float(x), 4) for x in b_equity],
        benchmark_stats=_stats(b_equity, b_held, b_trades, b_costs, b_turnover),
    )
