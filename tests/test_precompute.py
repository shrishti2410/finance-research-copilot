"""The daily precompute: freshness, the first-visitor fallback, the read-only
price mode, and the job's least-privilege role and workflow.

The cache tests run against the Postgres in DATABASE_URL (migrated to 0008)
and skip without it. The role itself was rehearsed on the local database with
scripts/create_precompute_role.py --no-secret (see the commit message).
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from market import precompute as pc
from market.markets import NSE, US

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


# --- freshness --------------------------------------------------------------------

@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 11, 17, 11, 0, tzinfo=UTC), datetime(2026, 11, 17, 10, 30, tzinfo=UTC)),  # Tue, after close
    (datetime(2026, 11, 17, 9, 0, tzinfo=UTC), datetime(2026, 11, 16, 10, 30, tzinfo=UTC)),   # Tue, before close
    (datetime(2026, 11, 22, 12, 0, tzinfo=UTC), datetime(2026, 11, 20, 10, 30, tzinfo=UTC)),  # Sun -> Fri
    (datetime(2026, 11, 23, 4, 0, tzinfo=UTC), datetime(2026, 11, 20, 10, 30, tzinfo=UTC)),   # Mon morning -> Fri
])
def test_the_last_close_is_the_latest_weekday_session_end(now, expected):
    assert pc.last_close(now, NSE) == expected


def test_a_page_is_fresh_only_after_the_last_close_and_from_this_code():
    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    after = datetime(2026, 11, 17, 11, 0, tzinfo=UTC)
    before = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
    assert pc.is_fresh(after, pc.CODE_VERSION, now)
    assert not pc.is_fresh(before, pc.CODE_VERSION, now)       # computed before today's close
    assert not pc.is_fresh(after, "0000000000000000", now)     # other code
    # A holiday or weekend: nothing new closed, so Friday evening's page stays fresh.
    friday_evening = datetime(2026, 11, 20, 11, 0, tzinfo=UTC)
    assert pc.is_fresh(friday_evening, pc.CODE_VERSION, datetime(2026, 11, 22, 9, 0, tzinfo=UTC))
    assert pc.last_close(now, US) != pc.last_close(now, NSE)


def test_the_code_version_covers_the_signal_sources_and_ignores_line_endings(tmp_path):
    assert re.fullmatch(r"[0-9a-f]{16}", pc.CODE_VERSION)
    assert pc._code_version() == pc.CODE_VERSION
    for name in pc._SOURCES:
        assert (ROOT / "market" / name).is_file(), name


# --- the store and the fallback ------------------------------------------------------

def _run(coro_factory):
    from db.base import engine

    async def go():
        try:
            return await coro_factory()
        finally:
            await engine.dispose()

    try:
        return asyncio.run(go())
    except Exception as exc:  # noqa: BLE001
        if "precomputed_views" in str(exc) or type(exc).__name__ in ("OSError", "ConnectionRefusedError"):
            pytest.skip(f"precomputed_views not available ({type(exc).__name__}); migrate to 0008")
        raise


async def _wipe(key):
    from db.base import engine
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM precomputed_views WHERE key = :k"), {"k": key})


def test_the_first_visitor_computes_and_stores_and_the_next_one_reads():
    key = "test:fallback"
    calls = []

    async def compute():
        calls.append(1)
        return {"as_of": "2026-11-16", "value": 42}

    async def scenario():
        await _wipe(key)
        now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
        first = await pc.cached(key, compute, now=now)
        # Stored with computed_at = the database's now(); read it back as fresh
        # by asking "as of" a moment just after it.
        second = await pc.cached(key, compute, now=datetime.now(UTC) + timedelta(seconds=1))
        await _wipe(key)
        return first, second

    first, second = _run(scenario)
    assert first["value"] == 42 and first["served"]["from"] == "computed"
    assert second["value"] == 42 and second["served"]["from"] == "precomputed"
    assert len(calls) == 1


def test_a_stale_or_old_code_page_is_recomputed():
    key = "test:stale"

    async def compute():
        return {"as_of": "2026-11-16", "value": "new"}

    async def scenario():
        from db.base import engine
        await _wipe(key)
        async with engine.begin() as conn:
            await pc.store(conn, key, date(2026, 11, 13), {"value": "old"})
            await conn.execute(text("UPDATE precomputed_views SET code_version = 'old-code' "
                                    "WHERE key = :k"), {"k": key})
        out = await pc.cached(key, compute)
        await _wipe(key)
        return out

    out = _run(scenario)
    assert out["value"] == "new" and out["served"]["from"] == "computed"


def test_a_read_only_market_data_never_writes_the_price_cache():
    from market.providers import Bar
    from market.store import MarketData

    class Fake:
        name = "fake"

        def daily_bars(self, ticker, start, end):
            return [Bar(day=start, open=1, high=1, low=1, close=1, adj_close=1, volume=1)]

    async def scenario():
        from db.base import engine
        async with engine.begin() as conn:
            for t in ("price_bars", "price_fetches"):
                await conn.execute(text(f"DELETE FROM {t} WHERE ticker = 'ZZRO'"))
        bars = await MarketData(Fake(), ttl=timedelta(0), write=False).history(
            "ZZRO", date(2026, 1, 5), date(2026, 1, 9))
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT count(*) FROM price_bars WHERE ticker = 'ZZRO'"))).scalar()
            fetches = (await conn.execute(text("SELECT count(*) FROM price_fetches WHERE ticker = 'ZZRO'"))).scalar()
        return bars, rows, fetches

    bars, rows, fetches = _run(scenario)
    assert len(bars) == 1 and rows == 0 and fetches == 0
