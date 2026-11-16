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

## Results

(Filled in after the run.)
