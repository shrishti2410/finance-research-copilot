"""Evaluate the signal engine out of sample on the watchlist, and show today's signals.

    python scripts/evaluate_signals.py --out benchmarks/results/2026-11-16-signals
    python scripts/evaluate_signals.py --years 5 SBIN.NS DIXON.NS

For each ticker: the walk-forward signal over the last --years years against
buy-and-hold, the market index and 200 random-timing runs (market/signal_eval.py),
and the current signal (market/signals.py). Prices come from the cache
(scripts/load_prices.py); no language model is involved.

Writes signals_eval.json (everything) to --out, and prints a summary table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.base import engine  # noqa: E402
from market.service import signal_evaluation, trading_signal  # noqa: E402
from market.universe import INDIA_WATCHLIST  # noqa: E402


def _pct(x):
    return "   n/a" if x is None else f"{x * 100:6.1f}"


def _num(x):
    return "   n/a" if x is None else f"{x:6.2f}"


async def main(tickers: list[str], years: int, out: Path | None,
               regime_rule: str | None = None) -> int:
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    started = time.perf_counter()
    results = {}
    try:
        for ticker in tickers:
            evaluation = await signal_evaluation(ticker, years=years, regime_rule=regime_rule)
            current = await trading_signal(ticker)
            results[ticker] = {"evaluation": evaluation, "current": current}
    finally:
        await engine.dispose()
    elapsed = time.perf_counter() - started

    print(f"Out of sample, last {years} years requested; Indian delivery costs. "
          f"CAGR and max drawdown (DD) in %; Sharpe = CAGR / annualised volatility.")
    head = f"{'CAGR':>6} {'Sharpe':>6} {'DD':>6}"
    print(f"{'ticker':<14}{'window':<25}{'years':>5}  {'signal':^20}  {'buy & hold':^20}  "
          f"{'Nifty 50':^20}  {'random (median)':^13}  {'pctl':>5}  {'up after BUY':>13}")
    print(f"{'':<44}  {head}  {head}  {head}  {'CAGR':>6} {'Sharpe':>6}  {'':>5}  "
          f"{'vs base':>13}")

    def trio(d):
        return f"{_pct(d.get('cagr'))} {_num(d.get('sharpe_ratio'))} {_pct(d.get('max_drawdown'))}"

    for ticker, r in results.items():
        e = r["evaluation"]
        if not e.get("ok"):
            print(f"{ticker:<14}{e.get('error')}")
            continue
        rnd, h = e["random"], e["hit_rate"]
        print(f"{ticker:<14}{e['start'] + ' to ' + e['end']:<25}{e['years']:>5}  "
              f"{trio(e['signal'])}  {trio(e['buy_and_hold'])}  {trio(e['nifty'] or {})}  "
              f"{_pct(rnd['cagr_median'])} {_num(rnd['sharpe_median'])}  "
              f"{_num(rnd['signal_sharpe_percentile'])}  "
              f"{_pct(h['after_buy_up'])} vs{_pct(h['base_rate_up'])}")
    print()
    print("Risk-adjusted, same window, every ticker. Sharpe = mean / sd of daily returns "
          "x sqrt(252); Sortino uses the downside deviation; Calmar = CAGR / |max DD|; "
          "no risk-free rate.")
    print(f"{'ticker':<14}  {'':>4}  {'Sharpe':>6} {'Sortino':>7} {'Calmar':>6} "
          f"{'in mkt':>6}  {'CAGR':>6} {'DD':>6}")
    for ticker, r in results.items():
        e = r["evaluation"]
        if not e.get("ok"):
            continue
        for label, side in (("signal", "signal"), ("b&h", "buy_and_hold"), ("nifty", "nifty")):
            m = e["risk"][side] or {}
            print(f"{ticker if label == 'signal' else '':<14}  {label:>6}"
                  f"{_num(m.get('sharpe'))} {_num(m.get('sortino')):>7} {_num(m.get('calmar'))} "
                  f"{_pct(m.get('time_in_market'))}  {_pct(m.get('cagr'))} "
                  f"{_pct(m.get('max_drawdown'))}")
    print()
    print("Today's signals (decided on the last completed bar)")
    for ticker, r in results.items():
        c = r["current"]
        if not c.get("ok"):
            print(f"  {ticker:<14}{c.get('error')}")
            continue
        warn = "  [limited history]" if c["history"]["limited"] else ""
        warn += "  [narrow vote]" if any(w.startswith("Narrow") for w in c["warnings"]) else ""
        print(f"  {ticker:<14}{c['as_of']}  {c['signal']:<4}  score {c['score']:+.3f}  "
              f"on {c['coverage']['text']:<15} confidence {c['confidence']:.3f}  "
              f"regime {c['regime']['label']:<13}{warn}")
    print(f"\n{len(tickers)} tickers in {elapsed:.1f}s, commit {commit}")

    if out:
        out.mkdir(parents=True, exist_ok=True)
        meta = {"command": " ".join(sys.argv), "commit": commit,
                "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "machine": platform.platform(), "python": platform.python_version(),
                "elapsed_s": round(elapsed, 1), "years": years,
                "regime_rule": regime_rule or "default"}
        (out / "signals_eval.json").write_text(
            json.dumps({"meta": meta, "results": results}, indent=1, default=str),
            encoding="utf-8")
        print(f"wrote {out / 'signals_eval.json'}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", default=list(INDIA_WATCHLIST))
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--regime-rule", choices=("slope", "agree"),
                    help="regime direction rule for the evaluation (market/regime.py); "
                         "today's signals always use the default")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.tickers, args.years, args.out, args.regime_rule)))
