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
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone

from market import analytics, indicators
from market.costs import cost_model_for
from market.markets import completed_bars, market_for, normalize_ticker
from market.store import MarketData

__all__ = ["chart_data", "price_analytics", "backtest", "strategy_catalogue",
           "WARMUP_CALENDAR_DAYS", "BACKTEST_WARMUP_CALENDAR_DAYS", "DEFAULT_BENCHMARK",
           "parse_indicators", "default_benchmark", "market_info", "trading_signal",
           "signal_evaluation", "option_ideas", "watchlist", "signal_report",
           "compute_watchlist", "compute_signal_report", "compute_option_ideas",
           "SIGNAL_HISTORY_CALENDAR_DAYS"]

# Enough trading days for the slowest indicator offered (SMA 200) plus the
# warm-up of the smoothed ones: 300 calendar days is about 205 trading days.
WARMUP_CALENDAR_DAYS = 300
# The slowest strategy rule reads 252 trading days (12-1 momentum): about 365
# calendar days, plus a margin.
BACKTEST_WARMUP_CALENDAR_DAYS = 400
# The signal engine's history: eight years plus the warm-up. Its first fully
# trained signal needs the slowest rule's warm-up (252 bars) plus two years of
# training; the evaluation then wants five years after that.
SIGNAL_HISTORY_CALENDAR_DAYS = round(365.25 * 8) + BACKTEST_WARMUP_CALENDAR_DAYS
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


# The chart endpoint's results, kept until the stock's market closes another
# session. The 2026-11-18 load test found the chart the slowest endpoint at
# Render's 0.1 CPU: every request re-read two years of bars and recomputed its
# indicators (benchmarks/LOAD_TEST_MARKET.md). Bounded: an entry is a few
# thousand points, and the container has 512 MB.
CHART_CACHE_SIZE = 24
_chart_cache: "OrderedDict[tuple, dict]" = OrderedDict()


def clear_chart_cache() -> None:
    _chart_cache.clear()


def chart_cache_size() -> int:
    return len(_chart_cache)


async def chart_data(ticker: str, start: date, end: date, names: list[str],
                     data: MarketData | None = None) -> dict:
    """Bars and indicator series for a chart.

    Without `data`, the result is cached per session: the key is the stock, the
    range, the indicators, the provider, the market's latest close and the
    signal code's version, so a new close or new code computes afresh. A result
    is kept only if it already holds that close's session (or the range ends
    before it): one computed just after the close, before the provider has the
    day's bar, would otherwise stand for the whole session."""
    key = None
    if data is None:
        from market.precompute import CODE_VERSION, last_close

        data = MarketData()
        symbol = normalize_ticker(ticker)
        close = last_close(datetime.now(timezone.utc), market_for(symbol))
        key = (symbol, start, end, tuple(names), data.provider.name, close.date(), CODE_VERSION)
        hit = _chart_cache.get(key)
        if hit is not None:
            _chart_cache.move_to_end(key)
            return hit
    result = await _compute_chart(ticker, start, end, names, data)
    if key is not None:
        session = key[5]
        last_bar = result["bars"][-1]["time"] if result["bars"] else None
        if end < session or (last_bar is not None and last_bar >= session.isoformat()):
            _chart_cache[key] = result
            while len(_chart_cache) > CHART_CACHE_SIZE:
                _chart_cache.popitem(last=False)
    return result


async def _compute_chart(ticker: str, start: date, end: date, names: list[str],
                         data: MarketData) -> dict:
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


async def _signal_bars(symbol: str, data: MarketData, now: datetime | None):
    end = date.today()
    bars = await data.history(symbol, end - timedelta(days=SIGNAL_HISTORY_CALENDAR_DAYS), end)
    return completed_bars(bars, market_for(symbol), now or datetime.now(timezone.utc))


async def trading_signal(ticker: str, data: MarketData | None = None,
                         now: datetime | None = None) -> dict:
    """Today's BUY / SELL / HOLD for `ticker`, with all its evidence
    (market/signals.py). Decided on the last completed bar."""
    from market.signals import signal_evidence, walk_forward

    data = data or MarketData()
    symbol = normalize_ticker(ticker)
    bars = await _signal_bars(symbol, data, now)
    if len(bars) < 2:
        raise LookupError(f"No price history for {symbol}.")
    market = market_for(symbol)
    out = signal_evidence(walk_forward(bars, market), symbol, market)
    return {**out, **market_info(symbol), "provider": data.provider.name}


async def signal_evaluation(ticker: str, years: int = 5, data: MarketData | None = None,
                            now: datetime | None = None, regime_rule: str | None = None) -> dict:
    """The signal's out-of-sample record over the last `years` years against
    buy-and-hold, the market index and random timing (market/signal_eval.py)."""
    from market.signal_eval import evaluate

    data = data or MarketData()
    symbol = normalize_ticker(ticker)
    bars = await _signal_bars(symbol, data, now)
    if len(bars) < 2:
        raise LookupError(f"No price history for {symbol}.")
    bench = default_benchmark(symbol)
    bench_bars = await _signal_bars(bench, data, now) if bench != symbol else None
    out = evaluate(bars, bench_bars, market_for(symbol), years=years, regime_rule=regime_rule)
    return {"ticker": symbol, "benchmark": bench, **market_info(symbol),
            "provider": data.provider.name, **out}


async def compute_option_ideas(ticker: str, structure: str | None = None, days: int = 30,
                       lot_size: int = 1, data: MarketData | None = None,
                       now: datetime | None = None) -> dict:
    """Option structures for `ticker`, priced with Black-Scholes from its own
    historical volatility (market/options.py). Without `structure`, the ones
    whose market view matches the signal engine's current output, which comes
    with its coverage and its backtest verdict: a structure that follows a
    signal with no edge inherits that."""
    from market.options import (
        DISCLAIMER,
        VOL_WINDOW,
        historical_volatility,
        option_structure,
        structures_for_signal,
    )
    from market.signal_eval import backtest_verdict, evaluate
    from market.signals import signal_evidence, walk_forward

    data = data or MarketData()
    symbol = normalize_ticker(ticker)
    bars = await _signal_bars(symbol, data, now)
    if len(bars) < VOL_WINDOW + 1:
        raise LookupError(f"Not enough price history for {symbol} to estimate volatility.")
    market = market_for(symbol)
    wf = walk_forward(bars, market)
    sig = signal_evidence(wf, symbol, market)
    if structure:
        keys = [structure]
    elif sig.get("ok"):
        keys = structures_for_signal(sig["signal"], sig["regime"]["label"])
    else:
        keys = structures_for_signal("HOLD")
    bench = default_benchmark(symbol)
    bench_bars = await _signal_bars(bench, data, now) if bench != symbol else None
    verdict = backtest_verdict(symbol, evaluate(bars, bench_bars, market, years=5, wf=wf))
    closes = [b.close for b in bars]          # traded (split-adjusted) prices
    spot, sigma = closes[-1], historical_volatility(closes)
    return {
        "ticker": symbol, **market_info(symbol), "as_of": bars[-1].day.isoformat(),
        "spot": round(spot, 2), "volatility_30d": round(sigma, 4),
        "signal": {k: sig.get(k) for k in ("signal", "score", "confidence")}
                  | {"coverage": (sig.get("coverage") or {}).get("text"),
                     "regime": (sig.get("regime") or {}).get("label")},
        "backtest": {k: verdict[k] for k in ("verdict", "statement")},
        "structures": [option_structure(k, spot, sigma, days=days, lot_size=lot_size)
                       for k in keys],
        "note": ("Structures whose market view matches the engine's output, for study. "
                 "Premiums are model estimates, not quotes."),
        "provider": data.provider.name, "disclaimer": DISCLAIMER,
    }


SPARKLINE_DAYS = 60


async def compute_watchlist(tickers=None, benchmark: str | None = None,
                            data: MarketData | None = None, now: datetime | None = None) -> dict:
    """One call for the dashboard: the index strips and a card per stock.

    Per ticker: the last close, the change on the previous close, the last
    SPARKLINE_DAYS closes, and for stocks the signal with its coverage and
    confidence. Prices are the traded (split-adjusted) close, as a quote
    would show; the signal uses its own adjusted series. `indices` holds one
    strip per market (Nifty 50, then SPY); `index` is the first of them, for
    clients written before the US stocks were added."""
    from market.signals import signal_evidence, walk_forward
    from market.universe import INDICES, SIGNALS_WATCHLIST

    data = data or MarketData()
    tickers = [normalize_ticker(t) for t in (tickers or SIGNALS_WATCHLIST)]
    indices = [normalize_ticker(benchmark)] if benchmark else list(INDICES)

    def card(symbol, bars):
        closes = [b.close for b in bars]
        last, prev = closes[-1], closes[-2] if len(closes) > 1 else None
        return {"ticker": symbol, **market_info(symbol), "as_of": bars[-1].day.isoformat(),
                "price": round(last, 2),
                "change_pct": round((last / prev - 1.0) * 100, 2) if prev else None,
                "sparkline": [round(c, 2) for c in closes[-SPARKLINE_DAYS:]]}

    strips = []
    for symbol in indices:
        bars = await _signal_bars(symbol, data, now)
        if len(bars) >= 2:
            strips.append(card(symbol, bars))
    cards = []
    for symbol in tickers:
        bars = await _signal_bars(symbol, data, now)
        if len(bars) < 2:
            cards.append({"ticker": symbol, "error": "no price history"})
            continue
        c = card(symbol, bars)
        market = market_for(symbol)
        ev = signal_evidence(walk_forward(bars, market), symbol, market)
        c["signal"] = ({"signal": ev["signal"], "score": ev["score"],
                        "confidence": ev["confidence"], "coverage": ev["coverage"]["text"],
                        "regime": ev["regime"]["label"],
                        "warnings": [w.split(":")[0] for w in ev["warnings"]]}
                       if ev.get("ok") else {"error": ev.get("error")})
        cards.append(c)
    return {"index": strips[0] if strips else None, "indices": strips, "stocks": cards,
            "provider": data.provider.name,
            "disclaimer": "Educational project, not investment advice."}


REPORT_CARD_VOTES = 4
REPORT_CARD_SECTIONS = ("Summary", "When it fails")
MARKER_YEARS = 3


def _changes(days, position, since) -> list[dict]:
    """The days the signal's position switched: "in" (went long) or "out"."""
    out = []
    for i in range(1, len(position)):
        if days[i] >= since and position[i] != position[i - 1]:
            out.append({"day": days[i].isoformat(), "action": "in" if position[i] > 0 else "out"})
    return out


async def compute_signal_report(ticker: str, data: MarketData | None = None,
                        now: datetime | None = None) -> dict:
    """Everything the stock page shows about the signal, in one call: the
    evidence, the backtest verdict with its curves and risk figures, the days
    the signal switched (for chart markers), and card excerpts for the
    heaviest votes."""
    from market.signal_eval import backtest_verdict, evaluate
    from market.signals import signal_evidence, walk_forward

    data = data or MarketData()
    symbol = normalize_ticker(ticker)
    bars = await _signal_bars(symbol, data, now)
    if len(bars) < 2:
        raise LookupError(f"No price history for {symbol}.")
    market = market_for(symbol)
    wf = walk_forward(bars, market)
    evidence = signal_evidence(wf, symbol, market)
    bench = default_benchmark(symbol)
    bench_bars = await _signal_bars(bench, data, now) if bench != symbol else None
    ev = evaluate(bars, bench_bars, market, years=5, wf=wf, curves=True)
    since = bars[-1].day - timedelta(days=round(365.25 * MARKER_YEARS))
    cards = await _card_excerpts([v["strategy"] for v in evidence.get("votes", [])[:REPORT_CARD_VOTES]])
    return {
        "ticker": symbol, **market_info(symbol), "provider": data.provider.name,
        "benchmark": bench,
        "evidence": evidence,
        "verdict": backtest_verdict(symbol, ev),
        "evaluation": {k: ev.get(k) for k in ("ok", "start", "end", "years", "risk", "random",
                                              "decisions", "curves", "error")},
        "markers": _changes(wf.days, wf.position, since),
        "cards": cards,
        "disclaimer": evidence.get("disclaimer", "Educational project, not investment advice."),
    }


async def _card_excerpts(keys: list[str]) -> list[dict]:
    """Summary and failure modes from the strategy_cards collection; empty if
    it is not indexed (the signal stands without its cards)."""
    from db.base import engine
    from rag.strategy_cards import card_sections

    out = []
    try:
        async with engine.connect() as conn:
            for key in keys:
                sections = await card_sections(conn, key)
                if sections:
                    out.append({"strategy": key, "source": f"docs/strategies/{key}.md",
                                **{name.lower().replace(" ", "_"): sections.get(name)
                                   for name in REPORT_CARD_SECTIONS}})
    except Exception:  # noqa: BLE001
        return []
    return out


# --- served from the daily precompute when fresh (market/precompute.py) -------

async def watchlist(tickers=None, benchmark: str | None = None,
                    data: MarketData | None = None, now: datetime | None = None) -> dict:
    """The dashboard: the stored copy when fresh, else computed and stored.
    Only the default watchlist is stored; any other list is computed."""
    from market import precompute

    if tickers is not None or benchmark is not None or data is not None:
        return await compute_watchlist(tickers, benchmark, data, now)
    return await precompute.cached(precompute.KEY_WATCHLIST,
                                   lambda: compute_watchlist(now=now), now=now)


def strip_entries(watchlist: dict) -> list[dict]:
    """The landing page's market strip from a stored watchlist: the indices,
    then each stock that loaded, with its price and day change only. No signal
    and no error text: the strip is public, and a stock that failed to load is
    left out rather than shown as a failure."""
    indices = watchlist.get("indices") or ([watchlist["index"]] if watchlist.get("index") else [])
    out = []
    for kind, cards in (("index", indices), ("stock", watchlist.get("stocks") or [])):
        for c in cards:
            if not c or c.get("error") or c.get("price") is None:
                continue
            out.append({"ticker": c["ticker"], "exchange": c.get("exchange"), "kind": kind,
                        "currency_symbol": c.get("currency_symbol"), "price": c["price"],
                        "change_pct": c.get("change_pct"), "as_of": c.get("as_of")})
    return out


async def signal_report(ticker: str, data: MarketData | None = None,
                        now: datetime | None = None) -> dict:
    from market import precompute

    if data is not None:
        return await compute_signal_report(ticker, data, now)
    symbol = normalize_ticker(ticker)
    return await precompute.cached(precompute.report_key(symbol),
                                   lambda: compute_signal_report(symbol, now=now),
                                   market=market_for(symbol), now=now)


async def option_ideas(ticker: str, structure: str | None = None, days: int = 30,
                       lot_size: int = 1, data: MarketData | None = None,
                       now: datetime | None = None) -> dict:
    """Stored only for the default view (the suggested structures, 30 days,
    per share); any other request is computed."""
    from market import precompute

    if structure or days != 30 or lot_size != 1 or data is not None:
        return await compute_option_ideas(ticker, structure, days, lot_size, data, now)
    symbol = normalize_ticker(ticker)
    return await precompute.cached(precompute.options_key(symbol),
                                   lambda: compute_option_ideas(symbol, now=now),
                                   market=market_for(symbol), now=now)

