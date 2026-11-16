"""Daily prices from a provider, cached in Postgres.

    data = MarketData()                       # Yahoo Finance by default
    bars = await data.history("NVDA", date(2025, 1, 1), date(2026, 1, 1))

When a request is served from the cache
---------------------------------------
A missing bar is ambiguous: a holiday, or a day nobody fetched. So the cache
records each range it asked the provider for (`price_fetches`), and a request
is served from Postgres only when one recorded fetch covers all of it and is
younger than `MARKET_CACHE_TTL_HOURS` (24 by default). Otherwise the whole
requested range is fetched again and upserted.

Why a time limit even for old history: `adj_close` is recomputed back through
history each time a company pays a dividend, so a cached adjusted series goes
stale on every ex-dividend date. A day-old copy is close enough for analytics
and charts; a week-old one may not be.

Writes are upserts keyed by (ticker, day), so refetching is idempotent.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from core.config import settings
from db.base import engine
from market.providers import Bar, PriceProvider, ProviderError, YFinanceProvider

__all__ = ["MarketData", "ProviderError", "normalize_ticker"]

_UPSERT = text("""
    INSERT INTO price_bars (ticker, day, open, high, low, close, adj_close, volume, provider)
    VALUES (:ticker, :day, :open, :high, :low, :close, :adj_close, :volume, :provider)
    ON CONFLICT (ticker, day) DO UPDATE SET
        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
        close = EXCLUDED.close, adj_close = EXCLUDED.adj_close,
        volume = EXCLUDED.volume, provider = EXCLUDED.provider
""")


def normalize_ticker(raw: str) -> str:
    """Upper case, and Yahoo's dash for a class share ("BRK.B" -> "BRK-B")."""
    return (raw or "").strip().upper().replace(".", "-")


class MarketData:
    def __init__(self, provider: PriceProvider | None = None,
                 ttl: timedelta | None = None) -> None:
        self.provider = provider or YFinanceProvider()
        self.ttl = ttl if ttl is not None else timedelta(hours=settings.market_cache_ttl_hours)

    async def _covered(self, conn, ticker: str, start: date, end: date) -> bool:
        cutoff = datetime.now(timezone.utc) - self.ttl
        found = (await conn.execute(text(
            "SELECT 1 FROM price_fetches WHERE ticker = :t AND provider = :p "
            "AND start_day <= :s AND end_day >= :e AND fetched_at >= :cutoff LIMIT 1"),
            {"t": ticker, "p": self.provider.name, "s": start, "e": end, "cutoff": cutoff},
        )).first()
        return found is not None

    async def _read(self, conn, ticker: str, start: date, end: date) -> list[Bar]:
        rows = (await conn.execute(text(
            "SELECT day, open, high, low, close, adj_close, volume FROM price_bars "
            "WHERE ticker = :t AND day BETWEEN :s AND :e ORDER BY day"),
            {"t": ticker, "s": start, "e": end})).all()
        return [Bar(day=r[0], open=float(r[1]), high=float(r[2]), low=float(r[3]),
                    close=float(r[4]), adj_close=float(r[5]), volume=int(r[6])) for r in rows]

    async def history(self, ticker: str, start: date, end: date) -> list[Bar]:
        """Daily bars from `start` to `end` inclusive, oldest first.

        Raises ProviderError when the cache cannot serve the range and the
        provider cannot be reached.
        """
        symbol = normalize_ticker(ticker)
        if start > end:
            raise ValueError(f"start {start} is after end {end}")
        async with engine.connect() as conn:
            if await self._covered(conn, symbol, start, end):
                return await self._read(conn, symbol, start, end)

        # The provider call is blocking network I/O; keep it off the event loop.
        bars = await asyncio.to_thread(self.provider.daily_bars, symbol, start, end)
        async with engine.begin() as conn:
            if bars:
                await conn.execute(_UPSERT, [
                    {"ticker": symbol, "day": b.day, "open": b.open, "high": b.high,
                     "low": b.low, "close": b.close, "adj_close": b.adj_close,
                     "volume": b.volume, "provider": self.provider.name} for b in bars])
            await conn.execute(text(
                "INSERT INTO price_fetches (ticker, start_day, end_day, provider, bars) "
                "VALUES (:t, :s, :e, :p, :n)"),
                {"t": symbol, "s": start, "e": end, "p": self.provider.name, "n": len(bars)})
        return [b for b in bars if start <= b.day <= end]
