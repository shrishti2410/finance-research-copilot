# Regime direction rule: a hold-out check

## Pre-registration (written and committed before anything below was run)

**Why.** In the first evaluation (`benchmarks/SIGNALS_7C.md`), SBIN.NS was
labelled `trending_down` on 2026-11-16 with ADX 42, +DI 38 and −DI 6. The
50-day average's 20-day slope was still negative after a rebound. The
direction measure lags a V-shaped turn.

**The change** (`market/regime.py`, rule `"agree"`). When ADX ≥ 25, a trend
label needs the slope and the DI lines to agree:
- `trending_up` when slope > 0 and +DI > −DI;
- `trending_down` when slope < 0 and −DI > +DI;
- `transition` otherwise.

The old rule (`"slope"`) uses the slope alone. Nothing else changes: ADX
levels, the volatility rule, weights, thresholds and costs stay the same.

**Where it is tested.** Ten NSE large and mid caps, none used so far for
anything (`market.universe.INDIA_HOLDOUT`):

> RELIANCE.NS, TCS.NS, ICICIBANK.NS, ITC.NS, LT.NS, SUNPHARMA.NS,
> VOLTAS.NS, TVSMOTOR.NS, CUMMINSIND.NS, PERSISTENT.NS

- Data: loaded with `scripts/load_prices.py --years 8`.
- Window: the same five-year out-of-sample window as the watchlist.
- Data failure: a ticker that fails the data check is reported and left out,
  not replaced.

**Decision rule.** Adopt `"agree"` only if, on the ten hold-out stocks, both
hold:
1. the signal's daily Sharpe (`risk.signal.sharpe`) is higher with `"agree"`
   than with `"slope"` on at least 6 of the 10;
2. the median change in that Sharpe is above zero.

The original five watchlist stocks are reported under both rules but do not
count toward the decision: the problem was found on them. Both results are
recorded whatever the outcome.

## Results (measured)

**Decision: not adopted.** On the hold-out:
- `"agree"` raised the signal's Sharpe on **3 of 10** stocks (the rule needed
  6);
- the median change was **0.000** (the rule needed above 0).

The default stays `"slope"`.

**How it was run:**

| | |
|---|---|
| commands | `python scripts/evaluate_signals.py --regime-rule {slope,agree} --out benchmarks/results/2026-11-16-regime-check/holdout-{rule} RELIANCE.NS TCS.NS ICICIBANK.NS ITC.NS LT.NS SUNPHARMA.NS VOLTAS.NS TVSMOTOR.NS CUMMINSIND.NS PERSISTENT.NS`, and the same without tickers (the watchlist) into `watchlist-{rule}` |
| commit | `afe350b` (the pre-registration commit), all four runs |
| date | 2026-11-16, 17:47–17:48 UTC |
| machine | ASUS Vivobook X1504ZA, Intel Core i5-1235U, Windows 11 (build 26300), Python 3.11.6 |
| data | Yahoo Finance via yfinance 1.2.0, loaded the same day with `scripts/load_prices.py --years 8`; all ten passed the data check (2,246 bars from 2017-10-12, largest gap 5 days) |
| awake | standby blocked with `SetThreadExecutionState`; the four runs took about a minute |
| raw output | `benchmarks/results/2026-11-16-regime-check/` (gitignored) |

Signal Sharpe below is daily: mean ÷ standard deviation of daily returns ×
√252, no risk-free rate. Window 2021-11-16 to 2026-11-16, Indian delivery
costs.

**Hold-out (decides):**

| ticker | Sharpe, slope | Sharpe, agree | change | CAGR slope / agree | max DD slope / agree | buy & hold Sharpe |
|---|---|---|---|---|---|---|
| RELIANCE.NS | −0.180 | −0.191 | −0.011 | −2.6% / −2.7% | −27.6% / −28.0% | 0.266 |
| TCS.NS | −0.637 | −0.637 | 0.000 | −6.2% / −6.2% | −35.2% / −35.2% | −0.106 |
| ICICIBANK.NS | 0.858 | 0.871 | +0.013 | 12.3% / 12.5% | −15.6% / −15.6% | 0.893 |
| ITC.NS | 0.735 | 0.712 | −0.023 | 9.3% / 9.0% | −22.7% / −23.8% | 0.574 |
| LT.NS | 0.692 | 0.692 | 0.000 | 11.7% / 11.7% | −21.1% / −21.1% | 0.793 |
| SUNPHARMA.NS | 0.528 | 0.570 | +0.042 | 6.7% / 7.3% | −30.2% / −28.1% | 1.019 |
| VOLTAS.NS | −0.129 | −0.070 | +0.058 | −3.7% / −2.7% | −42.1% / −39.0% | 0.124 |
| TVSMOTOR.NS | 1.044 | 1.003 | −0.041 | 21.7% / 20.5% | −29.3% / −29.3% | 1.501 |
| CUMMINSIND.NS | 1.171 | 1.159 | −0.012 | 25.9% / 25.6% | −15.5% / −15.5% | 1.237 |
| PERSISTENT.NS | 0.583 | 0.583 | 0.000 | 10.3% / 10.3% | −30.6% / −30.6% | 0.797 |

Higher with `"agree"` on 3 of 10; median change +0.000; mean +0.003.

**Watchlist (reported, does not decide):**

| ticker | Sharpe, slope | Sharpe, agree | change |
|---|---|---|---|
| COFORGE.NS | 0.268 | 0.268 | 0.000 |
| DIXON.NS | 0.741 | 0.741 | 0.000 |
| SBIN.NS | −0.075 | −0.080 | −0.006 |
| PREMIERENE.NS | −0.014 | −0.014 | 0.000 |
| HDFCBANK.NS | 0.138 | 0.136 | −0.002 |

Higher with `"agree"` on 0 of 5.

**What changed in the labels:**
- On the hold-out, `"agree"` moved 750 days out of the two trend labels into
  `transition`, about 6% of the 12,360 evaluated days. The decision counts
  barely moved: BUY 3,800 → 3,780, SELL 4,947 → 4,931.
- The rule makes the labels more coherent (no "down" with +DI far above −DI).
- That coherence did not show up in returns. The regime only scales the vote
  by halving reversion rules in a trend, and few votes sit near a threshold.

**The engine itself on fresh stocks.** The `slope` run is also the default
engine's first test on stocks it was never looked at with. Its signal was
below buy-and-hold on:
- CAGR on 10 of 10;
- daily Sharpe on 9 of 10 (ahead only on ITC);

and its drawdown was smaller on 7 of 10.

Against its random re-timings its percentile ranged from 0.10 to 0.80; none
reached 0.95. The finding in `benchmarks/SIGNALS_7C.md` holds out of sample:
no return edge, and smaller drawdowns, mostly from time out of the market.
