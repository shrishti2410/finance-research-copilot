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
from market.store import MarketData, normalize_ticker

__all__ = ["chart_data", "price_analytics", "WARMUP_CALENDAR_DAYS", "DEFAULT_BENCHMARK",
           "parse_indicators"]

# Enough trading days for the slowest indicator offered (SMA 200) plus the
# warm-up of the smoothed ones: 300 calendar days is about 205 trading days.
WARMUP_CALENDAR_DAYS = 300
DEFAULT_BENCHMARK = "SPY"
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
        "ticker": symbol, "start": start.isoformat(), "end": end.isoformat(),
        "provider": data.provider.name, "bars": bars, "series": series,
        "indicators": {n: indicators.INDICATORS[n] for n in names},
        "price_basis": "candles: split-adjusted traded prices; returns: dividend-adjusted close",
    }


async def price_analytics(ticker: str, start: date, end: date,
                          benchmark: str | None = DEFAULT_BENCHMARK,
                          data: MarketData | None = None) -> dict:
    data = data or MarketData()
    symbol = normalize_ticker(ticker)
    bars = await data.history(symbol, start, end)
    if len(bars) < 2:
        raise LookupError(f"No price history for {symbol} between {start} and {end}.")
    bench_bars = None
    bench = normalize_ticker(benchmark) if benchmark else None
    if bench and bench != symbol:
        bench_bars = await data.history(bench, start, end)
    out = analytics.summary(bars, bench_bars)
    return {"ticker": symbol, "benchmark": bench if bench_bars else None,
            "provider": data.provider.name, **out}
