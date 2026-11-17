# Quant honesty: no edge, and the smaller drawdowns are explained by lower exposure

The pre-registered comparison of [QUANT_HONESTY_RULES.md](QUANT_HONESTY_RULES.md),
applied as written. The rules were committed in `604a521` and the code in
`51a826a`, both before the first run on real prices; this record comes after.

## Verdicts

| rule | test | result |
|---|---|---|
| 1. replace the signal | an alternative needs +0.10 median Sharpe, a higher Sharpe on ≥10/15 stocks and on ≥7/10 hold-out stocks | **no alternative passes**. Each has a lower median Sharpe than the signal (table below). |
| 2. risk-adjusted edge | the signal has the higher Sharpe on ≥10/15 stocks and the median difference is > 0 | **no edge**: higher on 1 of 15; median Sharpe difference −0.23 |
| 3. "smaller drawdowns" | the signal's max drawdown is smaller than the **exposure-matched** benchmark's on ≥10/15 stocks | **claim withdrawn**: smaller on 2 of 15 |
| 4. recommendation | if rule 2 fails | present the signal as a **strategy-audit tool**, not a source of edge |
| 5. redundancy | pairs of rules with average correlation > 0.80 | **9 pairs**, one of them perfectly correlated (below) |

**What changes in what is published (proposed; not changed in this branch).**
- **The drawdown claim.** Against plain buy-and-hold, the signal's drawdown is
  smaller on 11 of 15 stocks, which is where the published claim came from.
  Against buy-and-hold held at the signal's own exposure, it is smaller on only
  2. So the smaller drawdowns are what being out of the market half the time
  gives anyway, not something the timing adds. Rule 3 requires withdrawing the
  claim where it appears:
  - the README's results table (line 181);
  - the "7 of 10 had a smaller max drawdown" line on /methodology
    (`frontend/src/content/site-metrics.json`);
  - `docs/SIGNALS.md` line 139.
- **"No edge" stays.** Rule 2 confirms it on all 15 stocks together.

## Medians across the 15 stocks

Net of costs, 5 years to the close of 2026-11-13; no risk-free rate.

| strategy | Sharpe | CAGR | max drawdown | exposure | turnover / yr | cost drag (CAGR pts) |
|---|---:|---:|---:|---:|---:|---:|
| **signal** (production, regime-weighted) | **0.44** | 4.4% | −31.0% | 44.6% | 6.1 | 1.07 |
| performance-weighted | 0.38 | 4.2% | −30.1% | 46.1% | 5.5 | 1.00 |
| equal-weight | 0.19 | 1.8% | −25.5% | 34.4% | 6.1 | 1.09 |
| majority | 0.04 | −0.6% | −26.6% | 31.3% | 12.8 | 2.08 |
| best rule, walk-forward | 0.22 | 2.4% | −25.1% | 45.8% | 10.2 | 1.66 |
| best rule, hindsight (look-ahead, a bound only) | 0.86 | 13.7% | −23.2% | 36.0% | 2.9 | 0.54 |
| buy-and-hold | 0.65 | 16.6% | −34.3% | 100% | 0.2 | 0.04 |
| exposure-matched buy-and-hold | 0.65 | 8.8% | −18.1% | 44.6% | 0 | n/a |

Reading these:
- **The ensembles.** Of the walk-forward ensembles, the production weighting
  has the best median Sharpe. The regime multipliers add a little over plain
  performance weighting (0.44 against 0.38). Every one of these, the signal
  included, is below buy-and-hold's 0.65.
- **The hindsight bound.** The best single rule chosen with hindsight would
  have done well (0.86), but choosing it walk-forward gives 0.22. The rule
  that looks best afterwards cannot be picked in advance. It was
  `rsi_reversion` on 7 of 15 stocks.
- **Exposure-matched Sharpe.** It equals buy-and-hold's by construction:
  scaling returns with 0% cash does not change the Sharpe ratio. This
  benchmark exists for the drawdown and CAGR comparison, where it shows that
  half the market's exposure gives half its CAGR and a shallower drawdown with
  no timing at all.
- **Costs.** Cost drag is about 1 CAGR point a year for the signal. Majority
  voting churns twice as much and pays twice the drag.

## Per stock

Sharpe ratios, net. The **bold** names are the 10 hold-out stocks.

| stock | signal | perf. | equal | majority | best (wf) | best (hindsight) | buy & hold |
|---|---:|---:|---:|---:|---:|---:|---:|
| COFORGE | 0.03 | 0.09 | 0.74 | 0.04 | 0.01 | 0.78 | 0.55 |
| DIXON | 1.08 | 1.07 | 0.19 | 0.15 | 0.66 | 1.32 | 0.65 |
| SBIN | −0.26 | −0.45 | 0.00 | 0.36 | 0.22 | 0.83 | 0.83 |
| PREMIERENE | 0.11 | −0.35 | 1.32 | −0.51 | −0.02 | 0.39 | 0.13 |
| HDFCBANK | −0.22 | 0.01 | −0.36 | −0.67 | −0.04 | 0.55 | 0.20 |
| **RELIANCE** | −0.25 | −0.27 | −0.51 | −0.41 | −0.61 | 0.86 | 0.24 |
| **TCS** | −0.83 | −0.73 | −0.13 | −0.05 | −0.36 | 0.20 | −0.11 |
| **ICICIBANK** | 0.75 | 0.57 | −0.09 | −0.28 | 0.32 | 1.14 | 0.88 |
| **ITC** | 0.44 | 0.41 | 0.85 | 0.80 | 0.60 | 1.01 | 0.53 |
| **LT** | 0.69 | 0.74 | −0.57 | −0.32 | 0.88 | 1.07 | 0.79 |
| **SUNPHARMA** | 0.49 | 0.38 | 0.31 | 0.47 | 0.48 | 1.13 | 1.01 |
| **VOLTAS** | −0.13 | −0.15 | 0.05 | −0.28 | −0.84 | 0.28 | 0.09 |
| **TVSMOTOR** | 0.94 | 0.84 | 0.82 | 1.07 | 1.08 | 1.51 | 1.51 |
| **CUMMINSIND** | 1.19 | 1.41 | 1.01 | 0.80 | 1.22 | 1.30 | 1.26 |
| **PERSISTENT** | 0.57 | 0.66 | 0.45 | 0.42 | 0.21 | 0.82 | 0.80 |

The signal's Sharpe is higher than buy-and-hold's only on DIXON, a watchlist
stock.

The signal against both benchmarks:

| stock | signal CAGR / max DD / exposure | buy & hold CAGR / max DD | exposure-matched CAGR / max DD |
|---|---|---|---|
| COFORGE | −1.5% / −36.7% / 46% | 14.1% / −46.7% | 8.0% / −24.2% |
| DIXON | 26.8% / −26.1% / 45% | 19.1% / −53.7% | 10.1% / −27.3% |
| SBIN | −6.0% / −34.1% / 49% | 18.8% / −23.9% | 9.6% / −11.9% |
| PREMIERENE | −0.1% / −33.1% / 38% | −1.3% / −39.3% | 0.9% / −16.6% |
| HDFCBANK | −2.1% / −24.1% / 21% | 2.0% / −31.7% | 0.8% / −7.2% |
| RELIANCE | −3.5% / −31.0% / 28% | 2.9% / −27.2% | 1.3% / −8.1% |
| TCS | −8.3% / −41.4% / 21% | −5.0% / −53.4% | −0.6% / −14.1% |
| ICICIBANK | 10.5% / −20.3% / 60% | 16.6% / −20.7% | 10.1% / −12.8% |
| ITC | 4.4% / −20.3% / 35% | 8.8% / −45.0% | 3.4% / −18.1% |
| LT | 11.7% / −21.1% / 60% | 17.8% / −28.9% | 11.2% / −18.2% |
| SUNPHARMA | 6.1% / −31.0% / 44% | 19.8% / −19.1% | 8.8% / −8.7% |
| VOLTAS | −3.9% / −43.2% / 33% | −1.9% / −50.5% | 0.4% / −18.7% |
| TVSMOTOR | 18.9% / −31.9% / 64% | 45.1% / −28.5% | 28.2% / −19.1% |
| CUMMINSIND | 26.5% / −15.5% / 60% | 39.2% / −34.3% | 23.1% / −21.5% |
| PERSISTENT | 10.2% / −29.3% / 47% | 24.5% / −36.0% | 12.4% / −18.4% |

## Rule correlation

The correlation of daily net returns, averaged over the 15 stocks. These pairs
are above 0.80:

| pair | correlation |
|---|---:|
| bollinger_reversion, zscore_reversion | **1.00** |
| atr_trailing_stop, ema_cross | 0.88 |
| ema_cross, obv_trend | 0.87 |
| price_above_sma, sma_cross | 0.87 |
| atr_trailing_stop, obv_trend | 0.84 |
| momentum, sma_cross | 0.84 |
| donchian_breakout, ema_cross | 0.82 |
| bollinger_breakout, donchian_breakout | 0.81 |
| bollinger_breakout, keltner_breakout | 0.80 |

- **Bollinger and z-score reversion are the same rule.** Correlation 1.00 means
  they take identical positions. A 20-day z-score below −2 is the price below
  the lower 2-standard-deviation band, so that rule's view is counted twice in
  the vote. **Inferred** from the definitions, not checked line by line.
- **The trend-following rules are a cluster:** ATR stop, EMA cross, OBV trend,
  Donchian, the SMA rules, momentum. So "12 of 16 rules agree" overstates how
  many independent views agree.
- **Nothing is removed on the strength of this run (rule 5).** Merging the
  duplicate is a separate proposal, with its own pre-registered test.

The full 16 x 16 matrix and the per-stock matrices are in the raw output.

## How it was run

| | |
|---|---|
| command | `python scripts/quant_honesty.py --out benchmarks/results/2026-11-17-quant-honesty` |
| commit | `51a826a` (`bench/quant-honesty`); rules `604a521` |
| date | 2026-11-17, finished 17:50:42 UTC, 46.9 s |
| machine | Windows 11 laptop (Python reports Windows 10, AMD64), Python 3.11.6, plugged in. Standby not blocked; the run took under a minute during an active session. |
| data | the local price cache (provider `yfinance`), bars to the close of 2026-11-13; each window 2021-11-15 to 2026-11-13 (4.9 years) |
| costs | Indian delivery costs and 5 bps slippage, next-open execution, as `market/signal_eval.py` |
| model | none: no language model is involved |

**Against the published results.** On its own window, this run's signal
figures differ slightly from [SIGNALS_7C.md](SIGNALS_7C.md), which ran to
2026-11-16. For example, SBIN's signal CAGR is −6.0% here against −5.3% there.
The verdicts are the same in both.

Raw output, gitignored: `benchmarks/results/2026-11-17-quant-honesty/`
(`quant_honesty.json` with every metric and matrix, and `run.log`).
