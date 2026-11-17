"""The public market strip on the landing page: GET /market/strip.

It needs no account, so it must be cheap and safe for anyone to call:
- it serves only the stored precomputed watchlist and never computes one (no
  price-provider call can come from an anonymous visitor);
- it returns prices and day changes only, with no signal;
- with nothing stored, or with the database unreachable, it answers with an
  error status and a fixed message, and the page hides the strip.

The pure-function tests need nothing. The route tests read and write one row
under a test key in the Postgres in DATABASE_URL, and skip without it.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from market import precompute as pc
from market.service import strip_entries

TEST_KEY = "test:strip"


def _card(ticker, exchange, price, change, **extra):
    return {"ticker": ticker, "exchange": exchange, "currency": "INR" if exchange == "NSE" else "USD",
            "currency_symbol": "₹" if exchange == "NSE" else "$", "as_of": "2026-11-17",
            "price": price, "change_pct": change, "sparkline": [1.0, 2.0, 3.0], **extra}


WATCHLIST = {
    "index": _card("^NSEI", "NSE", 25387.4, 0.38),
    "indices": [_card("^NSEI", "NSE", 25387.4, 0.38), _card("SPY", "US", 739.95, -1.91)],
    "stocks": [
        _card("SBIN.NS", "NSE", 1108.6, 1.66, signal={"signal": "BUY", "score": 0.4, "confidence": 0.5,
                                                      "coverage": "12 of 16", "regime": "trend",
                                                      "warnings": []}),
        _card("NVDA", "US", 156.14, -4.12),
        {"ticker": "LT.NS", "error": "The price provider could not be reached"},
    ],
    "provider": "yfinance",
    "disclaimer": "Not investment advice.",
}


# --- the payload ------------------------------------------------------------------

def test_the_strip_lists_the_indices_then_the_stocks_with_price_and_change_only():
    entries = strip_entries(WATCHLIST)
    assert [e["ticker"] for e in entries] == ["^NSEI", "SPY", "SBIN.NS", "NVDA"]
    assert entries[0] == {"ticker": "^NSEI", "exchange": "NSE", "kind": "index", "currency_symbol": "₹",
                          "price": 25387.4, "change_pct": 0.38, "as_of": "2026-11-17"}
    assert {e["kind"] for e in entries[2:]} == {"stock"}
    for e in entries:
        assert set(e) == {"ticker", "exchange", "kind", "currency_symbol", "price", "change_pct", "as_of"}


def test_a_stock_that_failed_to_load_is_left_out_not_shown_as_an_error():
    entries = strip_entries(WATCHLIST)
    assert "LT.NS" not in [e["ticker"] for e in entries]
    assert all("error" not in e and "signal" not in e for e in entries)


def test_an_older_payload_with_a_single_index_still_works():
    old = {k: v for k, v in WATCHLIST.items() if k != "indices"}
    assert [e["ticker"] for e in strip_entries(old)][:1] == ["^NSEI"]
    assert strip_entries({"index": None, "stocks": []}) == []


# --- the route --------------------------------------------------------------------

def _db(work):
    from db.base import engine

    async def go():
        try:
            return await work()
        finally:
            await engine.dispose()

    try:
        return asyncio.run(go())
    except Exception as exc:  # noqa: BLE001
        if "precomputed_views" in str(exc) or type(exc).__name__ in ("OSError", "ConnectionRefusedError"):
            pytest.skip(f"Postgres with precomputed_views not available ({type(exc).__name__})")
        raise


async def _wipe():
    from db.base import engine
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM precomputed_views WHERE key = :k"), {"k": TEST_KEY})


@pytest.fixture
def strip_key(monkeypatch):
    """Point the route at a test row, and make any attempt to compute a
    watchlist fail the test."""
    from market import service

    async def no_compute(*a, **k):
        raise AssertionError("the public strip must never compute a watchlist")

    monkeypatch.setattr(pc, "KEY_WATCHLIST", TEST_KEY)
    monkeypatch.setattr(service, "compute_watchlist", no_compute)
    _db(_wipe)
    yield
    _db(_wipe)


def _client():
    from api.main import app
    return TestClient(app)


def test_the_stored_watchlist_is_served_without_an_account(strip_key):
    from db.base import engine

    async def store():
        async with engine.begin() as conn:
            await pc.store(conn, TEST_KEY, date(2026, 11, 17), WATCHLIST)

    _db(store)
    with _client() as client:
        r = client.get("/market/strip")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["ticker"] for e in body["entries"]] == ["^NSEI", "SPY", "SBIN.NS", "NVDA"]
    assert body["served"]["from"] == "precomputed" and body["served"]["computed_at"]
    assert "signal" not in r.text and "BUY" not in r.text


def test_with_nothing_stored_it_answers_404_and_computes_nothing(strip_key):
    with _client() as client:
        r = client.get("/market/strip")
    assert r.status_code == 404
    assert r.json()["detail"] == "No market strip stored yet."


def test_with_the_database_down_it_answers_503_with_a_fixed_message(strip_key, monkeypatch):
    async def down(conn, key):
        raise OSError("connection to postgresql://user:secret@host refused")

    monkeypatch.setattr(pc, "load", down)
    with _client() as client:
        r = client.get("/market/strip")
    assert r.status_code == 503
    assert r.json()["detail"] == "The market strip is unavailable."
    assert "secret" not in r.text
