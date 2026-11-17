"""Stock pages computed once a day, served from Postgres.

On the free tier's 0.1 CPU the dashboard took 25-36 s and a stock page 30-40 s
(docs/DEPLOY_V2.md, measured 2026-11-17). Everything on them depends only on
daily closes, so a job computes them once after the NSE close
(scripts/precompute_views.py, run by .github/workflows/precompute-signals.yml)
and the API serves the stored result.

    payload = await cached(KEY_WATCHLIST, compute, market=NSE)

When a stored page is served
----------------------------
A row is fresh when both hold:

- it was computed after the most recent session close (the market's
  `session_close_utc` on the latest weekday). On a holiday nothing new closed,
  so yesterday's page is still the latest, and that is right;
- its `code_version` is this code's: a hash of the signal engine's sources, so
  a deploy that changes how signals are computed never serves pages from the
  old code.

Otherwise the page is computed as before and stored (`fallback`): a missed or
failed job costs the first visitor one slow page, not everyone.

Least privilege: the job's database role can read the price cache and the
strategy cards and write only `precomputed_views`. It therefore runs with
MarketData(write=False): stale prices are fetched from the provider into
memory, never written to the cache (deploy/neon-precompute-role.sql).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from sqlalchemy import text

from market.markets import NSE, Market

__all__ = ["CODE_VERSION", "KEY_WATCHLIST", "report_key", "options_key", "last_close",
           "is_fresh", "load", "store", "cached"]

log = logging.getLogger(__name__)

KEY_WATCHLIST = "watchlist"
TABLE = "precomputed_views"

# The sources whose behaviour a stored page depends on.
_SOURCES = ("analytics.py", "backtest.py", "costs.py", "indicators.py", "markets.py",
            "options.py", "regime.py", "service.py", "signal_eval.py", "signals.py",
            "strategies.py", "universe.py")


def _code_version() -> str:
    h = hashlib.sha256()
    here = Path(__file__).resolve().parent
    for name in _SOURCES:
        # Line endings normalised: the same commit checked out on Windows and
        # on the Linux job runner must hash the same.
        h.update((here / name).read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


CODE_VERSION = _code_version()


def report_key(ticker: str) -> str:
    return f"report:{ticker}"


def options_key(ticker: str) -> str:
    return f"options:{ticker}"


def last_close(now: datetime, market: Market = NSE) -> datetime:
    """The most recent weekday session close at or before `now` (UTC)."""
    now = now.astimezone(timezone.utc)
    day = now.date()
    while True:
        close = datetime.combine(day, market.session_close_utc, tzinfo=timezone.utc)
        if day.weekday() < 5 and close <= now:
            return close
        day -= timedelta(days=1)


def is_fresh(computed_at: datetime, code_version: str, now: datetime,
             market: Market = NSE) -> bool:
    return code_version == CODE_VERSION and computed_at >= last_close(now, market)


async def load(conn, key: str) -> dict | None:
    row = (await conn.execute(text(
        f"SELECT payload, as_of, computed_at, code_version FROM {TABLE} WHERE key = :k"),
        {"k": key})).first()
    if row is None:
        return None
    payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return {"payload": payload, "as_of": row[1], "computed_at": row[2], "code_version": row[3]}


async def store(conn, key: str, as_of, payload: dict) -> None:
    await conn.execute(text(
        f"INSERT INTO {TABLE} (key, as_of, code_version, payload, computed_at) "
        f"VALUES (:k, :a, :v, CAST(:p AS jsonb), now()) "
        f"ON CONFLICT (key) DO UPDATE SET as_of = EXCLUDED.as_of, "
        f"code_version = EXCLUDED.code_version, payload = EXCLUDED.payload, "
        f"computed_at = EXCLUDED.computed_at"),
        {"k": key, "a": as_of, "v": CODE_VERSION, "p": json.dumps(payload, default=str)})


def _as_of(payload: dict):
    from datetime import date

    raw = (payload.get("as_of") or (payload.get("evidence") or {}).get("as_of")
           or (payload.get("index") or {}).get("as_of"))
    return date.fromisoformat(raw) if raw else None


async def cached(key: str, compute: Callable[[], Awaitable[dict]], market: Market = NSE,
                 now: datetime | None = None) -> dict:
    """The stored page if fresh, else computed now and stored (best effort)."""
    from db.base import engine

    now = now or datetime.now(timezone.utc)
    try:
        async with engine.connect() as conn:
            row = await load(conn, key)
        if row and is_fresh(row["computed_at"], row["code_version"], now, market):
            return {**row["payload"], "served": {"from": "precomputed",
                                                 "computed_at": row["computed_at"].isoformat()}}
    except Exception as exc:  # noqa: BLE001  (a missing table must not break the page)
        log.warning("precompute: could not read %s (%s); computing", key, type(exc).__name__)
    payload = await compute()
    try:
        async with engine.begin() as conn:
            await store(conn, key, _as_of(payload), payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("precompute: could not store %s (%s)", key, type(exc).__name__)
    return {**payload, "served": {"from": "computed", "computed_at": now.isoformat()}}
