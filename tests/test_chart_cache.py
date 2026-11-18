"""The chart endpoint's per-day cache (market/service.py, chart_data).

The load test of 2026-11-18 (benchmarks/LOAD_TEST_MARKET.md) found the chart
the slowest endpoint: every request re-read two years of bars and recomputed
its indicators. The result is now kept in memory until the stock's market
closes another session, keyed by everything that shapes it. No database: the
data source is a stub.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from market import service
from market.providers import Bar

CLOSE = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)      # a US session close


class Stub:
    """A data source counting how often it is read."""

    calls = 0

    class provider:
        name = "stub"

    def __init__(self, last_day: date = CLOSE.date()):
        self.last_day = last_day

    async def history(self, symbol, start, end):
        Stub.calls += 1
        out, day, i = [], start, 0
        while day <= min(end, self.last_day):
            if day.weekday() < 5:
                p = 100 + i * 0.1
                out.append(Bar(day=day, open=p, high=p + 1, low=p - 1, close=p, adj_close=p, volume=1000))
                i += 1
            day += timedelta(days=1)
        return out


@pytest.fixture(autouse=True)
def stubbed(monkeypatch):
    from market import precompute

    service.clear_chart_cache()
    Stub.calls = 0
    monkeypatch.setattr(service, "MarketData", lambda: Stub())
    monkeypatch.setattr(precompute, "last_close", lambda now, market=None: CLOSE)
    yield
    service.clear_chart_cache()


def chart(ticker="NVDA", names=("sma50", "rsi14"), start=date(2025, 11, 17), end=date(2026, 11, 17)):
    return asyncio.run(service.chart_data(ticker, start, end, list(names)))


def test_the_same_chart_is_computed_once_per_session():
    first = chart()
    second = chart()
    assert Stub.calls == 1
    assert second == first


def test_anything_that_shapes_the_chart_is_part_of_the_key():
    chart()
    chart(names=("sma50",))
    chart(ticker="AAPL")
    chart(start=date(2024, 11, 18))
    assert Stub.calls == 4


def test_a_new_session_close_means_a_fresh_computation(monkeypatch):
    from market import precompute

    chart()
    monkeypatch.setattr(precompute, "last_close", lambda now, market=None: CLOSE + timedelta(days=1))
    chart(end=date(2026, 11, 18))
    assert Stub.calls == 2


def test_a_chart_missing_the_latest_session_is_not_kept(monkeypatch):
    # Computed just after the close, before the provider has that day's bar.
    monkeypatch.setattr(service, "MarketData", lambda: Stub(last_day=CLOSE.date() - timedelta(days=1)))
    chart()
    chart()
    assert Stub.calls == 2


def test_the_cache_is_bounded():
    for i in range(service.CHART_CACHE_SIZE + 5):
        chart(start=date(2025, 1, 1) + timedelta(days=i))
    assert service.chart_cache_size() == service.CHART_CACHE_SIZE


def test_a_caller_with_its_own_data_source_bypasses_the_cache():
    asyncio.run(service.chart_data("NVDA", date(2025, 11, 17), date(2026, 11, 17), ["sma50"], data=Stub()))
    asyncio.run(service.chart_data("NVDA", date(2025, 11, 17), date(2026, 11, 17), ["sma50"], data=Stub()))
    assert Stub.calls == 2 and service.chart_cache_size() == 0
