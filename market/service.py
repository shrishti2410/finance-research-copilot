"""What the API, the chart page and the agent ask the market layer for.

Two calls, shared so the chart a user opens and the chart the agent returns
are computed the same way:

    chart_data(ticker, start, end, indicators)   bars and indicator series
    price_analytics(ticker, start, end, benchmark)   returns, risk, beta

Indicators need history before the first bar shown: a 200-day average has no
value for its first 199 days. So `chart_data` fetches a warm-up period before
`start` (WARMUP_CALENDAR_DAYS), computes over all of it, and returns only the
requested range. Without that, every overlay would start blank on the left.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from market import analytics, indicators
from market.costs import cost_model_for
from market.markets import market_for, normalize_ticker
from market.store import MarketData

__all__ = ["chart_data", "price_analytics", "backtest", "strategy_catalogue",
           "WARMUP_CALENDAR_DAYS", "BACKTEST_WARMUP_CALENDAR_DAYS", "DEFAULT_BENCHMARK",
           "parse_indicators", "default_benchmark", "market_info"]

# Enough trading days for the slowest indicator offered (SMA 200) plus the
# warm-up of the smoothed ones: 300 calendar days is about 205 trading days.
WARMUP_CALENDAR_DAYS = 300
# The slowest strategy rule reads 252 trading days (12-1 momentum): about 365
# calendar days, plus a margin.
BACKTEST_WARMUP_CALENDAR_DAYS = 400
DEFAULT_BENCHMARK = "SPY"
# Each market's broad index, the default comparison for a stock that trades there.
BENCHMARKS = {"US": "SPY", "NSE": "^NSEI", "BSE": "^BSESN"}
MAX_INDICATORS = 8


def parse_indicators(raw) -> list[str]:
    """Indicator names from a list or a comma-separated string, validated."""
    if not raw:
        return []
    names = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    names = [n.strip().lower() for n in names if n and n.strip()]
    unknown = [n for n in names if n not in indicators.INDICATORS]
    if unknown:
        raise ValueError(f"Unknown indicator(s): {', '.join(unknown)}. "
                         f"Known: {', '.join(indicators.INDICATORS)}.")
    if len(names) > MAX_INDICATORS:
        raise ValueError(f"At most {MAX_INDICATORS} indicators at once.")
    return list(dict.fromkeys(names))


def default_benchmark(ticker: str) -> str:
    return BENCHMARKS.get(market_for(ticker).exchange, DEFAULT_BENCHMARK)


def market_info(ticker: str) -> dict:
    m = market_for(ticker)
    return {"exchange": m.exchange, "currency": m.currency, "currency_symbol": m.currency_symbol}


def _num(value: float) -> float | None:
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else round(float(value), 6)


async def chart_data(ticker: str, start: date, end: date, names: list[str],
                     data: MarketData | None = None) -> dict:
    data = data or MarketData()
    symbol = normalize_ticker(ticker)
    warm = await data.history(symbol, start - timedelta(days=WARMUP_CALENDAR_DAYS), end)
    shown = [i for i, b in enumerate(warm) if b.day >= start]
    first = shown[0] if shown else len(warm)
    series: dict[str, list[dict]] = {}
    for name in names:
        for key, values in indicators.compute(name, warm).items():
            series[key] = [{"time": warm[i].day.isoformat(), "value": _num(values[i])}
                           for i in range(first, len(warm)) if _num(values[i]) is not None]
    bars = [{"time": b.day.isoformat(), "open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "adj_close": b.adj_close, "volume": b.volume}
            for b in warm[first:]]
    return {
        "ticker": symbol, **market_info(symbol),
        "start": start.isoformat(), "end": end.isoformat(),
        "provider": data.provider.name, "bars": bars, "series": series,
        "indicators": {n: indicators.INDICATORS[n] for n in names},
        "price_basis": "candles: split-adjusted traded prices; returns: dividend-adjusted close",
    }


async def price_analytics(ticker: str, start: date, end: date,
                          benchmark: str | None = "auto",
                          data: MarketData | None = None) -> dict:
    """`benchmark="auto"` compares with the stock's own market index (SPY for
    a US stock, the Nifty 50 for an NSE one); None or "" compares with none."""
    data = data or MarketData()
    symbol = normalize_ticker(ticker)
    if benchmark == "auto":
        benchmark = default_benchmark(symbol)
    bars = await data.history(symbol, start, end)
    if len(bars) < 2:
        raise LookupError(f"No price history for {symbol} between {start} and {end}.")
    bench_bars = None
    bench = normalize_ticker(benchmark) if benchmark else None
    if bench and bench != symbol:
        bench_bars = await data.history(bench, start, end)
    out = analytics.summary(bars, bench_bars)
    return {"ticker": symbol, **market_info(symbol),
            "benchmark": bench if bench_bars else None,
            "provider": data.provider.name, **out}


def strategy_catalogue() -> list[dict]:
    from market.strategies import STRATEGIES
    return [{"key": st.key, "name": st.name, "description": st.description,
             "params": st.params, "allows_short": st.allows_short}
            for st in STRATEGIES.values()]


async def backtest(ticker: str, strategy: str, start: date, end: date,
                   params: dict | None = None, cost_bps: float = 5.0,
                   slippage_bps: float = 5.0, initial_cash: float = 10_000.0,
                   data: MarketData | None = None, cost_model: str | None = None) -> dict:
    """Run one strategy on `ticker` from `start` to `end`, with a warm-up
    before `start` that the strategy reads but does not trade.

    The cost model follows the ticker's market unless `cost_model` names one:
    Indian delivery charges for an NSE or BSE stock (long or flat only),
    `cost_bps` per side otherwise."""
    from dataclasses import asdict

    from market.backtest import run_backtest
    from market.strategies import get_strategy

    get_strategy(strategy)                     # an unknown key fails before any fetch
    data = data or MarketData()
    symbol = normalize_ticker(ticker)
    bars = await data.history(symbol, start - timedelta(days=BACKTEST_WARMUP_CALENDAR_DAYS), end)
    model = cost_model_for(cost_model or market_for(symbol).cost_model, cost_bps)
    result = run_backtest(bars, strategy, params, initial_cash=initial_cash,
                          cost_bps=cost_bps, slippage_bps=slippage_bps, start_day=start,
                          cost_model=model)
    return {
        "ticker": symbol, **market_info(symbol), "provider": data.provider.name,
        "costs": model.describe(),
        "strategy": result.strategy, "params": result.params,
        "start": result.days[0], "end": result.days[-1],
        "cost_bps": cost_bps, "slippage_bps": slippage_bps, "initial_cash": initial_cash,
        "days": result.days, "equity": result.equity,
        "benchmark_equity": result.benchmark_equity, "positions": result.positions,
        "trades": [asdict(t) for t in result.trades],
        "stats": result.stats, "benchmark_stats": result.benchmark_stats,
        "execution": "decided at the close, filled at the next open; prices adjusted "
                     "for dividends and splits",
    }
