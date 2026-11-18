"""Run the pre-registered double-vote comparison (benchmarks/RULE_DEDUP_RULES.md).

    python scripts/rule_dedup.py --out benchmarks/results/2026-11-18-rule-dedup

Production against variant A (zscore_reversion dropped) and variant B (one
vote per correlated cluster, from each refit's training window) on the 15 NSE
stocks, 5 years to the close of 2026-11-13, and the adoption rules applied as
written. No language model is involved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.base import engine  # noqa: E402
from market.ablation import agreement, full_metrics, rule_return_series, variant_positions  # noqa: E402
from market.costs import cost_model_for  # noqa: E402
from market.markets import NSE  # noqa: E402
from market.service import SIGNAL_HISTORY_CALENDAR_DAYS  # noqa: E402
from market.signals import walk_forward  # noqa: E402
from market.store import MarketData  # noqa: E402
from market.universe import INDIA_HOLDOUT, INDIA_WATCHLIST  # noqa: E402
from scripts.quant_honesty import END, YEARS, _higher, _strip  # noqa: E402

DUP = ("bollinger_reversion", "zscore_reversion")


async def one(ticker: str, data: MarketData) -> dict:
    model = cost_model_for(NSE.cost_model)
    bars = [b for b in await data.history(ticker, END - timedelta(days=SIGNAL_HISTORY_CALENDAR_DAYS), END)
            if b.day <= END]
    wf = walk_forward(bars, NSE, model)
    wanted = END - timedelta(days=round(365.25 * YEARS))
    start = max(wf.first, next(i for i, b in enumerate(bars) if b.day >= wanted))
    day0 = bars[start].day
    returns = rule_return_series(bars, wf, model)
    series = {
        "production": wf.position,
        "A_drop_duplicate": variant_positions(wf, NSE, exclude=frozenset({"zscore_reversion"})),
        "B_clusters": variant_positions(wf, NSE, returns=returns),
    }
    out = {"ok": True, "start": day0.isoformat(), "end": bars[-1].day.isoformat(),
           "strategies": {k: _strip(full_metrics(bars, p, day0, model)) for k, p in series.items()},
           "duplicate_agreement": round(agreement(wf.targets[DUP[0]][start:], wf.targets[DUP[1]][start:]), 4),
           "duplicate_days": len(bars) - start,
           "positions_changed_by_A": int(np.sum(series["A_drop_duplicate"][start:] != wf.position[start:])),
           "positions_changed_by_B": int(np.sum(series["B_clusters"][start:] != wf.position[start:]))}
    return out


def verdicts(results: dict) -> dict:
    tickers = list(results)
    holdout = [t for t in tickers if t in INDIA_HOLDOUT]

    def sh(t, k):
        return results[t]["strategies"][k]["sharpe"]

    med = {k: float(np.median([sh(t, k) for t in tickers if sh(t, k) is not None]))
           for k in ("production", "A_drop_duplicate", "B_clusters")}
    days = sum(results[t]["duplicate_days"] for t in tickers)
    agree = sum(results[t]["duplicate_agreement"] * results[t]["duplicate_days"] for t in tickers) / days
    a_ok = agree >= 0.99 and med["A_drop_duplicate"] >= med["production"] - 0.05
    base = "A_drop_duplicate" if a_ok else "production"
    wins = sum(_higher(sh(t, "B_clusters"), sh(t, base)) for t in tickers)
    wins_ho = sum(_higher(sh(t, "B_clusters"), sh(t, base)) for t in holdout)
    b_ok = med["B_clusters"] - med[base] >= 0.10 and wins >= 10 and wins_ho >= 7
    return {
        "median_sharpe": {k: round(v, 4) for k, v in med.items()},
        "rule1_A": {"pooled_agreement": round(agree, 4), "agreement_needed": 0.99,
                    "median_sharpe_change": round(med["A_drop_duplicate"] - med["production"], 4),
                    "adopted": a_ok},
        "rule2_B": {"against": base, "median_sharpe_change": round(med["B_clusters"] - med[base], 4),
                    "higher_sharpe": f"{wins} of {len(tickers)}",
                    "higher_sharpe_holdout": f"{wins_ho} of {len(holdout)}", "adopted": b_ok},
    }


async def main(out_dir: Path | None) -> int:
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    started = time.perf_counter()
    data = MarketData()
    results = {}
    try:
        for t in (*INDIA_WATCHLIST, *INDIA_HOLDOUT):
            results[t] = await one(t, data)
            r = results[t]
            print(f"{t:<15} agree {r['duplicate_agreement']:.4f}  changed A {r['positions_changed_by_A']:4d} "
                  f"B {r['positions_changed_by_B']:4d}  Sharpe prod {r['strategies']['production']['sharpe']} "
                  f"A {r['strategies']['A_drop_duplicate']['sharpe']} B {r['strategies']['B_clusters']['sharpe']}",
                  flush=True)
    finally:
        await engine.dispose()
    v = verdicts(results)
    print(json.dumps(v, indent=2))
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        meta = {"command": " ".join(["python", *sys.argv]), "commit": commit,
                "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "machine": f"{platform.system()} {platform.release()}", "python": platform.python_version(),
                "seconds": round(time.perf_counter() - started, 1), "end": END.isoformat()}
        (out_dir / "rule_dedup.json").write_text(json.dumps({"meta": meta, "results": results, "verdicts": v},
                                                            indent=1, default=str), encoding="utf-8")
        print(f"wrote {out_dir / 'rule_dedup.json'}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path)
    raise SystemExit(asyncio.run(main(ap.parse_args().out)))
