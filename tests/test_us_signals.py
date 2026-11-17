"""NVDA, AAPL and MSFT in the signals watchlist (backlog item 6), so one stock
page has both 10-K research and signals. Synthetic prices: no network; the
precompute keys and the US benchmark need no database."""

from __future__ import annotations

import asyncio

from market import service
from market.universe import (
    INDIA_WATCHLIST,
    SIGNALS_WATCHLIST,
    US_BENCHMARK,
    US_WATCHLIST,
)
from tests.test_markets_costs import _Recorder


def _run(coro):
    """asyncio.run, then dispose the shared engine: the card excerpts read the
    database, and pooled connections left on a closed event loop broke the
    next test file's health check (test_user_isolation skipped as
    "Postgres unreachable ... AttributeError")."""
    from db.base import engine

    async def go():
        try:
            return await coro
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_the_signals_watchlist_is_the_indian_five_and_three_us_stocks():
    assert US_WATCHLIST == ("NVDA", "AAPL", "MSFT") and US_BENCHMARK == "SPY"
    assert SIGNALS_WATCHLIST == (*INDIA_WATCHLIST, *US_WATCHLIST)
    assert service.default_benchmark("NVDA") == "SPY"


def test_the_dashboard_has_both_indices_and_us_cards_in_dollars():
    out = _run(service.compute_watchlist(data=_Recorder()))
    assert [c["ticker"] for c in out["stocks"]] == list(SIGNALS_WATCHLIST)
    assert [i["ticker"] for i in out["indices"]] == ["^NSEI", "SPY"]
    assert out["index"]["ticker"] == "^NSEI"                # kept for older clients
    nvda = next(c for c in out["stocks"] if c["ticker"] == "NVDA")
    assert (nvda["currency"], nvda["currency_symbol"], nvda["exchange"]) == ("USD", "$", "US")
    assert nvda["signal"]["signal"] in ("BUY", "SELL", "HOLD")


def test_a_us_stock_page_is_benchmarked_on_spy_and_may_vote_short():
    out = _run(service.compute_signal_report("NVDA", data=_Recorder()))
    assert out["benchmark"] == "SPY" and out["currency"] == "USD"
    assert out["evidence"]["ok"]
    voters = {v["strategy"] for v in out["evidence"]["votes"]}
    skipped = set(out["evidence"]["skipped_strategies"])
    assert "sma_cross_long_short" in voters | skipped              # a candidate on a US stock
    assert "sma_cross_long_short" not in out["evidence"]["skipped_strategies"] or \
        "no overnight shorts" not in out["evidence"]["skipped_strategies"]["sma_cross_long_short"]


def test_the_daily_job_precomputes_every_watchlist_stock():
    from market import precompute
    keys = precompute.job_keys()
    assert keys[0] == precompute.KEY_WATCHLIST
    for ticker in SIGNALS_WATCHLIST:
        assert precompute.report_key(ticker) in keys and precompute.options_key(ticker) in keys
    assert len(keys) == 1 + 2 * len(SIGNALS_WATCHLIST)
