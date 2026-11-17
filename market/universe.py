"""The stocks the signals page covers, and how much history each one has.

    INDIA_WATCHLIST   five NSE stocks, benchmarked against the Nifty 50
    US_WATCHLIST      NVDA, AAPL and MSFT, benchmarked against SPY: their 10-Ks are
                      indexed, so their stock pages have both research and signals
    SIGNALS_WATCHLIST both, in that order: the dashboard and the daily precompute

History is measured from the data, not written down: PREMIERENE.NS listed in
September 2024, so it has well under the five years the others have, and the
signal engine must know that rather than quietly fit on a short sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = ["INDIA_WATCHLIST", "INDIA_HOLDOUT", "INDIA_BENCHMARK", "US_WATCHLIST", "US_BENCHMARK",
           "SIGNALS_WATCHLIST", "INDICES", "HISTORY_YEARS", "History",
           "history_of", "FULL_HISTORY_TRADING_DAYS"]

INDIA_WATCHLIST = ("COFORGE.NS", "DIXON.NS", "SBIN.NS", "PREMIERENE.NS", "HDFCBANK.NS")
INDIA_BENCHMARK = "^NSEI"
US_WATCHLIST = ("NVDA", "AAPL", "MSFT")
US_BENCHMARK = "SPY"
SIGNALS_WATCHLIST = (*INDIA_WATCHLIST, *US_WATCHLIST)
INDICES = (INDIA_BENCHMARK, US_BENCHMARK)
# Ten NSE large and mid caps, never used to build or tune anything: chosen on
# 2026-11-16 before the regime-rule check was run, to test a change proposed
# after looking at the watchlist's results. See benchmarks/REGIME_RULE_CHECK.md.
INDIA_HOLDOUT = ("RELIANCE.NS", "TCS.NS", "ICICIBANK.NS", "ITC.NS", "LT.NS",
                 "SUNPHARMA.NS", "VOLTAS.NS", "TVSMOTOR.NS", "CUMMINSIND.NS",
                 "PERSISTENT.NS")
HISTORY_YEARS = 5
# Below this many trading days (about three years) a stock is "limited history".
FULL_HISTORY_TRADING_DAYS = 750
TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class History:
    first_day: date | None
    last_day: date | None
    trading_days: int
    largest_gap_days: int          # calendar days between consecutive bars
    limited: bool

    @property
    def years(self) -> float:
        return round(self.trading_days / TRADING_DAYS_PER_YEAR, 2)


def history_of(bars) -> History:
    if not bars:
        return History(None, None, 0, 0, True)
    gaps = [(b.day - a.day).days for a, b in zip(bars, bars[1:])]
    return History(bars[0].day, bars[-1].day, len(bars), max(gaps, default=0),
                   len(bars) < FULL_HISTORY_TRADING_DAYS)
