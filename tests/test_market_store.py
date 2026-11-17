"""The price cache: a provider is asked once per range, Postgres serves the rest.

Runs against live Postgres (migrated to 0006) with a fake provider, so nothing
reaches Yahoo Finance. Skips when the database is unreachable.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from market.providers import Bar, PriceProvider, ProviderError, YFinanceProvider
from market.store import MarketData, normalize_ticker

TICKER = "ZZPRICE"


class FakeProvider:
    name = "fake"

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def daily_bars(self, ticker, start, end):
        self.calls.append((ticker, start, end))
        if self.fail:
            raise ProviderError("down")
        out, day, price = [], start, 100.0
        while day <= end:
            if day.weekday() < 5:                 # no weekend bars, like a market
                out.append(Bar(day=day, open=price, high=price + 1, low=price - 1,
                               close=price, adj_close=price * 0.98, volume=1000))
                price += 1
            day += timedelta(days=1)
        return out


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def clean():
    from db.base import engine

    async def wipe():
        await engine.dispose()
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM price_bars WHERE ticker = :t"), {"t": TICKER})
            await conn.execute(text("DELETE FROM price_fetches WHERE ticker = :t"), {"t": TICKER})
        await engine.dispose()

    try:
        run(wipe())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"price tables not available ({type(exc).__name__}); migrate to 0006")
    yield
    run(wipe())


def history(data, start, end):
    from db.base import engine

    async def go():
        try:
            return await data.history(TICKER, start, end)
        finally:
            await engine.dispose()
    return run(go())


def test_the_yfinance_provider_satisfies_the_interface():
    assert isinstance(YFinanceProvider(), PriceProvider)
    assert isinstance(FakeProvider(), PriceProvider)


def test_a_first_request_fetches_and_a_second_is_served_from_postgres(clean):
    provider = FakeProvider()
    data = MarketData(provider)
    first = history(data, date(2026, 1, 5), date(2026, 1, 30))
    second = history(data, date(2026, 1, 12), date(2026, 1, 23))
    assert len(provider.calls) == 1, "the second range is inside the first"
    assert len(first) == 20 and len(second) == 10
    wanted = [b for b in first if date(2026, 1, 12) <= b.day <= date(2026, 1, 23)]
    # Prices are stored as NUMERIC(18, 6): equal to six decimal places.
    assert [b.day for b in second] == [b.day for b in wanted]
    assert all(abs(a.adj_close - b.adj_close) < 1e-6 and a.close == b.close
               for a, b in zip(second, wanted))


def test_a_range_outside_what_was_fetched_is_fetched(clean):
    provider = FakeProvider()
    data = MarketData(provider)
    history(data, date(2026, 1, 5), date(2026, 1, 9))
    history(data, date(2026, 1, 5), date(2026, 1, 16))
    assert len(provider.calls) == 2


def test_an_expired_fetch_is_fetched_again_and_overwrites(clean):
    provider = FakeProvider()
    history(MarketData(provider, ttl=timedelta(0)), date(2026, 1, 5), date(2026, 1, 9))
    history(MarketData(provider, ttl=timedelta(0)), date(2026, 1, 5), date(2026, 1, 9))
    assert len(provider.calls) == 2

    from db.base import engine

    async def count():
        async with engine.connect() as conn:
            n = (await conn.execute(text(
                "SELECT COUNT(*) FROM price_bars WHERE ticker = :t"), {"t": TICKER})).scalar()
        await engine.dispose()
        return n
    assert run(count()) == 5, "a refetch upserts; it does not duplicate"


def test_both_price_kinds_survive_the_round_trip(clean):
    data = MarketData(FakeProvider())
    history(data, date(2026, 1, 5), date(2026, 1, 9))
    cached = history(data, date(2026, 1, 5), date(2026, 1, 9))
    assert cached[0].close == 100.0 and cached[0].adj_close == pytest.approx(98.0)


def test_a_provider_failure_is_raised_and_nothing_is_recorded(clean):
    data = MarketData(FakeProvider(fail=True))
    with pytest.raises(ProviderError):
        history(data, date(2026, 1, 5), date(2026, 1, 9))
    provider = FakeProvider()
    history(MarketData(provider), date(2026, 1, 5), date(2026, 1, 9))
    assert len(provider.calls) == 1, "the failed attempt left no fetch record behind"


def test_an_inverted_range_is_refused():
    with pytest.raises(ValueError):
        run(MarketData(FakeProvider()).history(TICKER, date(2026, 2, 1), date(2026, 1, 1)))


@pytest.mark.parametrize("raw, symbol", [("aapl", "AAPL"), (" brk.b ", "BRK-B")])
def test_tickers_are_normalised(raw, symbol):
    assert normalize_ticker(raw) == symbol


def test_a_refetch_that_omits_a_cached_bar_does_not_lose_it(monkeypatch):
    """Yahoo, at midnight in India, returned histories without that day's
    session. The cached bar must survive the short response."""
    import asyncio as _asyncio
    from datetime import timedelta as _td

    from db.base import engine as _engine
    from market.providers import Bar as _Bar
    from market.store import MarketData as _MarketData

    days = [date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16)]

    class Shrinking:
        name = "fake"
        calls = 0

        def daily_bars(self, ticker, start, end):
            Shrinking.calls += 1
            use = days if Shrinking.calls == 1 else days[:-1]       # second call drops the last day
            return [_Bar(day=d, open=1.0, high=1.0, low=1.0, close=float(i + 1),
                         adj_close=float(i + 1), volume=1) for i, d in enumerate(use)]

    async def go():
        from sqlalchemy import text as _text
        try:
            async with _engine.begin() as conn:
                for table in ("price_bars", "price_fetches"):
                    await conn.execute(_text(f"DELETE FROM {table} WHERE ticker = 'ZZSHORT'"))
            data = _MarketData(Shrinking(), ttl=_td(0))              # always refetch
            first = await data.history("ZZSHORT", days[0], days[-1] + _td(days=1))
            second = await data.history("ZZSHORT", days[0], days[-1] + _td(days=1))
            async with _engine.begin() as conn:
                for table in ("price_bars", "price_fetches"):
                    await conn.execute(_text(f"DELETE FROM {table} WHERE ticker = 'ZZSHORT'"))
            return first, second
        finally:
            await _engine.dispose()

    try:
        first, second = _asyncio.run(go())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"price tables not available ({type(exc).__name__}); migrate to 0006")
    assert [b.day for b in first] == days
    assert [b.day for b in second] == days and second[-1].close == 4.0


def test_another_providers_rows_are_never_read_into_this_series():
    """A test's fake feed wrote SPY rows into a shared cache; on days the real
    provider has no bar they were merged into the real series."""
    import asyncio as _asyncio
    from datetime import timedelta as _td

    from db.base import engine as _engine
    from market.providers import Bar as _Bar
    from market.store import MarketData as _MarketData

    class Named:
        def __init__(self, name, price, days):
            self.name, self.price, self.days = name, price, days

        def daily_bars(self, ticker, start, end):
            return [_Bar(day=d, open=self.price, high=self.price, low=self.price,
                         close=self.price, adj_close=self.price, volume=1) for d in self.days]

    real_days = [date(2026, 9, 4), date(2026, 9, 8)]
    fake_days = [date(2026, 9, 4), date(2026, 9, 7), date(2026, 9, 8)]   # 9-7: a US holiday

    async def go():
        from sqlalchemy import text as _text
        try:
            async with _engine.begin() as conn:
                for table in ("price_bars", "price_fetches"):
                    await conn.execute(_text(f"DELETE FROM {table} WHERE ticker = 'ZZMIX'"))
            await _MarketData(Named("fake", 200.0, fake_days), ttl=_td(0)).history(
                "ZZMIX", date(2026, 9, 1), date(2026, 9, 9))
            real = await _MarketData(Named("yfinance", 700.0, real_days), ttl=_td(0)).history(
                "ZZMIX", date(2026, 9, 1), date(2026, 9, 9))
            async with _engine.begin() as conn:
                for table in ("price_bars", "price_fetches"):
                    await conn.execute(_text(f"DELETE FROM {table} WHERE ticker = 'ZZMIX'"))
            return real
        finally:
            await _engine.dispose()

    try:
        real = _asyncio.run(go())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"price tables not available ({type(exc).__name__})")
    assert [b.day for b in real] == real_days and {b.close for b in real} == {700.0}
