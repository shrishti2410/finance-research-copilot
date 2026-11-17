"""Compute the dashboard and the watchlist's stock pages, and store them.

The watchlist is market.universe.SIGNALS_WATCHLIST: the five NSE stocks and
NVDA, AAPL, MSFT (keys: market.precompute.job_keys()).

    python scripts/precompute_views.py            # DATABASE_URL from the environment

Run daily after the NSE close by .github/workflows/precompute-signals.yml,
with a database role that can read the price cache and the strategy cards and
write only `precomputed_views` (deploy/neon-precompute-role.sql). Prices it
has to fetch are kept in memory, never written to the cache.

Prints one line per page and its time; never the connection string. Exits 1
if any page failed, so the workflow run shows red.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.base import engine  # noqa: E402
from market import precompute  # noqa: E402
from market.service import (  # noqa: E402
    compute_option_ideas,
    compute_signal_report,
    compute_watchlist,
)
from market.store import MarketData  # noqa: E402
from market.universe import SIGNALS_WATCHLIST  # noqa: E402


async def main() -> int:
    data = MarketData(write=False)
    jobs = [(precompute.KEY_WATCHLIST, lambda: compute_watchlist(data=data))]
    for ticker in SIGNALS_WATCHLIST:
        jobs.append((precompute.report_key(ticker), lambda t=ticker: compute_signal_report(t, data=data)))
        jobs.append((precompute.options_key(ticker), lambda t=ticker: compute_option_ideas(t, data=data)))
    print(f"code version {precompute.CODE_VERSION}; {len(jobs)} pages", flush=True)
    failed, started = 0, time.perf_counter()
    try:
        for key, compute in jobs:
            t = time.perf_counter()
            try:
                payload = await compute()
                as_of = precompute._as_of(payload)
                async with engine.begin() as conn:
                    await precompute.store(conn, key, as_of, payload)
                print(f"  {key:<26} as of {as_of}  {time.perf_counter() - t:5.1f} s", flush=True)
            except Exception as exc:  # noqa: BLE001  (one failed page must not stop the rest)
                failed += 1
                print(f"  {key:<26} FAILED: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
    finally:
        await engine.dispose()
    print(f"{len(jobs) - failed} stored, {failed} failed, {time.perf_counter() - started:.1f} s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
