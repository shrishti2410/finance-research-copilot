"""Run the pre-registered quant-honesty comparison (benchmarks/QUANT_HONESTY_RULES.md).

    python scripts/quant_honesty.py --out benchmarks/results/2026-11-17-quant-honesty

For the 15 NSE stocks of SIGNALS_7C, over the 5 years to the close of
2026-11-13: the production signal, four other ensembles and the hindsight best
rule, against buy-and-hold and the exposure-matched benchmark, with the full
metrics; the 16 rules' correlation; and the adoption rules applied
mechanically. Prices come from the price cache; no language model is involved.

Writes quant_honesty.json to --out and prints the tables and the verdicts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.base import engine  # noqa: E402
from market.ablation import (  # noqa: E402
    ENSEMBLES,
    correlation,
    ensemble_positions,
    exposure_matched,
    full_metrics,
    rule_returns,
)
from market.costs import cost_model_for  # noqa: E402
from market.markets import NSE  # noqa: E402
from market.service import SIGNAL_HISTORY_CALENDAR_DAYS  # noqa: E402
from market.signal_eval import risk_metrics  # noqa: E402
from market.signals import walk_forward  # noqa: E402
from market.store import MarketData  # noqa: E402
from market.universe import INDIA_HOLDOUT, INDIA_WATCHLIST  # noqa: E402

END = date(2026, 11, 13)
YEARS = 5
NAMES = {"regime": "signal (regime-weighted)", "performance": "performance-weighted",
         "equal": "equal-weight", "majority": "majority", "best_rule": "best rule (walk-forward)"}


def _strip(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_")}


async def one(ticker: str, data: MarketData) -> dict:
    model = cost_model_for(NSE.cost_model)
    bars = await data.history(ticker, END - timedelta(days=SIGNAL_HISTORY_CALENDAR_DAYS), END)
    bars = [b for b in bars if b.day <= END]
    wf = walk_forward(bars, NSE, model)
    wanted = END - timedelta(days=round(365.25 * YEARS))
    start = max(wf.first, next(i for i, b in enumerate(bars) if b.day >= wanted))
    if len(bars) - start < 60:
        return {"ok": False, "error": f"only {len(bars) - start} bars after the first signal"}
    day0 = bars[start].day

    out = {"ok": True, "start": day0.isoformat(), "end": bars[-1].day.isoformat(),
           "years": round((len(bars) - start) / 252, 2), "strategies": {}}
    for kind in ENSEMBLES:
        out["strategies"][kind] = _strip(full_metrics(bars, ensemble_positions(wf, NSE, kind), day0, model))

    ones = np.ones(len(bars))
    bh = full_metrics(bars, ones, day0, model)
    out["strategies"]["buy_and_hold"] = _strip(bh)
    e = out["strategies"]["regime"]["exposure"] or 0.0
    em = risk_metrics(exposure_matched(bh["_equity"], e))
    out["strategies"]["exposure_matched"] = {
        "cagr": em["cagr"], "max_drawdown": em["max_drawdown"], "sharpe": em["sharpe"],
        "sortino": em["sortino"], "exposure": round(e, 4), "turnover_per_year": 0.0,
        "cost_drag": None}

    # The hindsight best rule: the best Sharpe over this very window (look-ahead).
    per_rule = {k: _strip(full_metrics(bars, wf.targets[k], day0, model)) for k in wf.candidates}
    best = max(per_rule, key=lambda k: (per_rule[k]["sharpe"] if per_rule[k]["sharpe"] is not None
                                        else -np.inf, k))
    out["strategies"]["best_rule_hindsight"] = {**per_rule[best], "rule": best}
    out["rules"] = per_rule

    keys, c = correlation(rule_returns(bars, wf, start, model))
    out["correlation"] = {"rules": keys, "matrix": [[None if np.isnan(x) else round(float(x), 4)
                                                     for x in row] for row in c]}
    return out


def _higher(a, b) -> bool:
    return a is not None and (b is None or a > b)


def verdicts(results: dict) -> dict:
    """The adoption rules of QUANT_HONESTY_RULES.md, applied as written."""
    tickers = list(results)
    holdout = [t for t in tickers if t in INDIA_HOLDOUT]
    ok = [t for t in tickers if results[t]["ok"]]

    def sharpe(t, k):
        return results[t]["strategies"][k]["sharpe"] if results[t]["ok"] else None

    out = {"evaluated": len(ok), "of": len(tickers),
           "not_evaluated": {t: results[t]["error"] for t in tickers if not results[t]["ok"]}}
    # Rule 1: an alternative replaces the signal only if (a), (b) and (c) hold.
    base = [sharpe(t, "regime") for t in ok]
    rule1 = {}
    for k in ("performance", "equal", "majority", "best_rule"):
        alt = [sharpe(t, k) for t in ok]
        med_diff = (float(np.median([a for a in alt if a is not None]))
                    - float(np.median([b for b in base if b is not None])))
        wins = sum(_higher(sharpe(t, k), sharpe(t, "regime")) for t in tickers)
        wins_ho = sum(_higher(sharpe(t, k), sharpe(t, "regime")) for t in holdout)
        rule1[k] = {"median_sharpe_minus_signal": round(med_diff, 4), "higher_sharpe": f"{wins} of {len(tickers)}",
                    "higher_sharpe_holdout": f"{wins_ho} of {len(holdout)}",
                    "passes": med_diff >= 0.10 and wins >= 10 and wins_ho >= 7}
    out["rule1_replace_signal"] = rule1
    # Rule 2: edge over buy-and-hold.
    diffs = [sharpe(t, "regime") - sharpe(t, "buy_and_hold") for t in ok
             if sharpe(t, "regime") is not None and sharpe(t, "buy_and_hold") is not None]
    wins = sum(_higher(sharpe(t, "regime"), sharpe(t, "buy_and_hold")) for t in tickers)
    out["rule2_edge"] = {"higher_sharpe_than_buy_and_hold": f"{wins} of {len(tickers)}",
                         "median_sharpe_difference": round(float(np.median(diffs)), 4) if diffs else None,
                         "edge": wins >= 10 and bool(diffs) and float(np.median(diffs)) > 0}
    # Rule 3: smaller drawdowns than the exposure-matched benchmark.
    smaller = 0
    for t in tickers:
        if not results[t]["ok"]:
            continue
        s = results[t]["strategies"]
        if s["regime"]["max_drawdown"] is not None and s["exposure_matched"]["max_drawdown"] is not None \
                and s["regime"]["max_drawdown"] > s["exposure_matched"]["max_drawdown"]:
            smaller += 1          # drawdowns are negative: larger is shallower
    out["rule3_drawdown"] = {"smaller_than_exposure_matched": f"{smaller} of {len(tickers)}",
                             "claim_stands": smaller >= 10}
    out["rule4_recommendation"] = (None if out["rule2_edge"]["edge"] else
                                   "present the signal as a strategy-audit tool, not a source of edge")
    # Rule 5: redundant pairs, from the correlation averaged over the stocks.
    mats = [np.array(results[t]["correlation"]["matrix"], dtype=float) for t in ok]
    keys = results[ok[0]]["correlation"]["rules"] if ok else []
    avg = np.nanmean(np.stack(mats), axis=0) if mats else np.zeros((0, 0))
    out["average_correlation"] = {"rules": keys,
                                  "matrix": [[None if np.isnan(x) else round(float(x), 3) for x in row]
                                             for row in avg]}
    out["rule5_redundant_pairs"] = sorted(
        [{"pair": [keys[i], keys[j]], "corr": round(float(avg[i, j]), 3)}
         for i in range(len(keys)) for j in range(i + 1, len(keys)) if avg[i, j] > 0.80],
        key=lambda p: -p["corr"])
    return out


def _f(x, pct=False):
    if x is None:
        return "   n/a"
    return f"{x * 100:6.1f}" if pct else f"{x:6.2f}"


async def main(out_dir: Path | None) -> int:
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    started = time.perf_counter()
    data = MarketData()
    results = {}
    try:
        for t in (*INDIA_WATCHLIST, *INDIA_HOLDOUT):
            t0 = time.perf_counter()
            try:
                results[t] = await one(t, data)
            except Exception as exc:  # noqa: BLE001  (reported, counted as not evaluated)
                results[t] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            print(f"{t:<15} {time.perf_counter() - t0:6.1f} s", flush=True)
    finally:
        await engine.dispose()
    v = verdicts(results)

    cols = ("regime", "performance", "equal", "majority", "best_rule", "best_rule_hindsight",
            "buy_and_hold", "exposure_matched")
    print("\nSharpe (net, no risk-free rate), 5 years to 2026-11-13")
    print(f"{'ticker':<15}" + "".join(f"{c[:11]:>12}" for c in cols))
    for t, r in results.items():
        row = "".join(f"{_f(r['strategies'][c]['sharpe']):>12}" for c in cols) if r["ok"] else "  " + r["error"]
        print(f"{t:<15}{row}")
    print("\nSignal against its benchmarks: CAGR %, max DD %, exposure %, turnover/yr, cost drag %")
    for t, r in results.items():
        if not r["ok"]:
            continue
        s, b, e = r["strategies"]["regime"], r["strategies"]["buy_and_hold"], r["strategies"]["exposure_matched"]
        print(f"{t:<15} signal {_f(s['cagr'], 1)} {_f(s['max_drawdown'], 1)} {_f(s['exposure'], 1)} "
              f"{s['turnover_per_year']:5.1f} {_f(s['cost_drag'], 1)} | b&h {_f(b['cagr'], 1)} "
              f"{_f(b['max_drawdown'], 1)} | exp-matched {_f(e['cagr'], 1)} {_f(e['max_drawdown'], 1)}")
    print("\n" + json.dumps({k: v[k] for k in v if k != "average_correlation"}, indent=2))

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        meta = {"command": " ".join(["python", *sys.argv]), "commit": commit,
                "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "machine": f"{platform.system()} {platform.release()}, {platform.machine()}",
                "python": platform.python_version(), "seconds": round(time.perf_counter() - started, 1),
                "end": END.isoformat(), "years": YEARS, "provider": data.provider.name}
        (out_dir / "quant_honesty.json").write_text(
            json.dumps({"meta": meta, "results": results, "verdicts": v}, indent=1, default=str),
            encoding="utf-8")
        print(f"\nwrote {out_dir / 'quant_honesty.json'}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path)
    raise SystemExit(asyncio.run(main(ap.parse_args().out)))
