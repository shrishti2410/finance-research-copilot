"""Load daily prices for the signals watchlist into the price cache.

    python scripts/load_prices.py                  # the NSE watchlist + ^NSEI, 5 years
    python scripts/load_prices.py --years 5 SBIN.NS ^NSEI

Fetches HISTORY_YEARS of daily bars, plus the warm-up the slowest strategy
rule reads (market.service.BACKTEST_WARMUP_CALENDAR_DAYS), through the same
MarketData cache the API uses, then reports per ticker: rows, first and last
day, the largest gap between bars, and whether the history is limited.

A gap of more than a week would mean missing data (a long exchange holiday
is at most a few days), so the script exits non-zero when it finds one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.base import engine  # noqa: E402
from market.markets import market_for  # noqa: E402
from market.service import BACKTEST_WARMUP_CALENDAR_DAYS  # noqa: E402
from market.store import MarketData  # noqa: E402
from market.universe import HISTORY_YEARS, INDIA_BENCHMARK, INDIA_WATCHLIST, history_of  # noqa: E402

MAX_GAP_DAYS = 7


async def main(tickers: list[str], years: int) -> int:
    end = date.today()
    start = end - timedelta(days=round(365.25 * years) + BACKTEST_WARMUP_CALENDAR_DAYS)
    data, bad = MarketData(), 0
    print(f"{'ticker':<15}{'ccy':<5}{'rows':>6}  {'first':<11}{'last':<11}{'gap':>4}  note")
    try:
        for ticker in tickers:
            bars = await data.history(ticker, start, end)
            h = history_of(bars)
            note = "limited history" if h.limited else ""
            if h.largest_gap_days > MAX_GAP_DAYS:
                note, bad = f"GAP of {h.largest_gap_days} days", bad + 1
            if not bars:
                note, bad = "NO DATA", bad + 1
            print(f"{ticker:<15}{market_for(ticker).currency:<5}{h.trading_days:>6}  "
                  f"{h.first_day!s:<11}{h.last_day!s:<11}{h.largest_gap_days:>4}  {note}")
    finally:
        await engine.dispose()
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", default=[*INDIA_WATCHLIST, INDIA_BENCHMARK])
    ap.add_argument("--years", type=int, default=HISTORY_YEARS)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.tickers, args.years)))
