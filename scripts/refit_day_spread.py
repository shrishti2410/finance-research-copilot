"""How much does the day of the month the weights are refitted on move the result?

    python scripts/refit_day_spread.py --out benchmarks/results/<date>-refit-spread
    python scripts/refit_day_spread.py SBIN.NS DIXON.NS --days 20

Runs the five-year out-of-sample evaluation once per refit day (the 1st, 2nd,
... 20th trading day of each month) and reports, per stock, the range of the
signal's CAGR and daily Sharpe, against buy-and-hold's (which does not
depend on it). The spread is the noise an arbitrary scheduling choice puts
into any single evaluation: a difference smaller than it is not a finding.
No random baseline is run (it does not depend on the refit day).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.base import engine  # noqa: E402
from market.markets import market_for  # noqa: E402
from market.service import SIGNAL_HISTORY_CALENDAR_DAYS, default_benchmark  # noqa: E402
from market.signal_eval import evaluate  # noqa: E402
from market.store import MarketData  # noqa: E402
from market.universe import INDIA_HOLDOUT, INDIA_WATCHLIST  # noqa: E402


async def main(tickers, days, out):
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    data = MarketData()
    end = date.today()
    start = end - timedelta(days=SIGNAL_HISTORY_CALENDAR_DAYS)
    started = time.perf_counter()
    rows = {}
    try:
        for t in tickers:
            bars = await data.history(t, start, end)
            bench = await data.history(default_benchmark(t), start, end)
            runs = []
            for k in range(days):
                ev = evaluate(bars, bench, market_for(t), years=5, n_random=1, refit_day=k)
                runs.append({"refit_day": k, "signal": ev["risk"]["signal"],
                             "buy_and_hold": ev["risk"]["buy_and_hold"],
                             "start": ev["start"], "end": ev["end"]})
            rows[t] = runs
    finally:
        await engine.dispose()

    print(f"Signal over the last five years, one run per refit day (1st..{days}th trading "
          f"day of the month). Daily Sharpe; CAGR in %.")
    print(f"{'ticker':<14} {'Sharpe min':>10} {'median':>7} {'max':>6} {'b&h':>6}   "
          f"{'CAGR min':>8} {'median':>7} {'max':>6} {'b&h':>6}   {'runs beating b&h on both':>24}")
    for t, runs in rows.items():
        sh = [r["signal"]["sharpe"] for r in runs]
        cg = [r["signal"]["cagr"] * 100 for r in runs]
        bsh = runs[0]["buy_and_hold"]["sharpe"]
        bcg = runs[0]["buy_and_hold"]["cagr"] * 100
        beat = sum(1 for r in runs if r["signal"]["sharpe"] > bsh and r["signal"]["cagr"] * 100 > bcg)
        print(f"{t:<14} {min(sh):>10.2f} {st.median(sh):>7.2f} {max(sh):>6.2f} {bsh:>6.2f}   "
              f"{min(cg):>8.1f} {st.median(cg):>7.1f} {max(cg):>6.1f} {bcg:>6.1f}   "
              f"{beat:>18} of {len(runs)}")
    elapsed = time.perf_counter() - started
    print(f"\n{len(tickers)} tickers x {days} refit days in {elapsed:.0f}s, commit {commit}")
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / "refit_spread.json").write_text(json.dumps(
            {"meta": {"command": " ".join(sys.argv), "commit": commit, "days": days,
                      "elapsed_s": round(elapsed, 1)}, "results": rows}, indent=1),
            encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", default=[*INDIA_WATCHLIST, *INDIA_HOLDOUT])
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    asyncio.run(main(args.tickers, args.days, args.out))
