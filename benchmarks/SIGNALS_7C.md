# The signal engine out of sample: five NSE stocks, 2021-11-16 to 2026-11-16

Step 7C, phase 3. The method is in `market/signals.py`, the evaluation in
`market/signal_eval.py`, the design in `docs/SIGNALS.md`.

**In short:**
- The signal did not beat buy-and-hold on return for any of the four stocks
  with five years of history.
- It beat its own randomly re-timed copies decisively on none of them.
- What it did do is spend half the time or less in the market. That cut the
  maximum drawdown on three of the four stocks, at the cost of most of the
  return.
- No evidence here that the timing carries information.

## How it was run

| | |
|---|---|
| command | `python scripts/evaluate_signals.py --out benchmarks/results/2026-11-16-signals` |
| commit | `dd2dbb8` on `feat/v2-market` (engine committed in `86a1f01` before it was first run; `dd2dbb8` only changes the printed table) |
| date | 2026-11-16, 17:24 UTC |
| machine | ASUS Vivobook X1504ZA, Intel Core i5-1235U, Windows 11 (build 26300), Python 3.11.6, NumPy 2.4.3 |
| data | Yahoo Finance daily bars through yfinance 1.2.0, loaded into the local pgvector Postgres price cache the same day with `python scripts/load_prices.py --years 8` (2017-10-12 to 2026-11-16; PREMIERENE.NS from its listing on 2024-09-03) |
| model | none: no language model is involved in computing a signal |
| awake | standby blocked with `SetThreadExecutionState` for the run; it took 7.7 s |
| raw output | `benchmarks/results/2026-11-16-signals/` (gitignored): `signals_eval.json`, `stdout.txt` |

The same command was run first on `86a1f01` and gave the same numbers: the
engine has no randomness except the seeded random baseline.

## Settings, fixed before the first run

- **Training:** 504 days (126 for limited history); weights refitted every 21
  days.
- **Thresholds:** BUY at a score of +0.25 or more, SELL at −0.25 or less.
- **Regime:** ADX 25 and 20; volatility percentile 0.80. A rule whose style
  does not fit the regime gets half weight.
- **Costs and fills:** Indian delivery costs (`market/costs.py`) plus 5 bps
  slippage, on a ₹1,00,000 account. BUY goes long at the next open, SELL goes
  flat, HOLD keeps the position.
- **Random baseline:** 200 circular shifts of the signal's own out-of-sample
  positions, seed 20261116.
- **Not changed after:** none of these was changed after seeing the results.

## Results (measured)

CAGR and max drawdown in %. Sharpe here is CAGR ÷ annualised volatility, with
no risk-free rate (the backtester's definition), so it is comparable across
columns but not with a published Sharpe ratio. "pctl" is the share of the 200
random-timing runs whose Sharpe was lower than the signal's.

```
ticker        window                   years         signal              buy & hold             Nifty 50        random (median)   pctl   up after BUY
                                                CAGR Sharpe     DD    CAGR Sharpe     DD    CAGR Sharpe     DD    CAGR Sharpe               vs base
COFORGE.NS    2021-11-16 to 2026-11-16   4.9     3.5   0.17  -37.0    13.3   0.37  -46.7     7.3   0.52  -16.5     5.8   0.24    0.39    64.7 vs  59.1
DIXON.NS      2021-11-16 to 2026-11-16   4.9    16.1   0.67  -27.9    19.1   0.49  -53.7     7.3   0.52  -16.5     7.6   0.30    0.76    56.2 vs  56.8
SBIN.NS       2021-11-16 to 2026-11-16   4.9    -3.0  -0.16  -29.9    19.7   0.80  -23.9     7.3   0.52  -16.5     8.8   0.51    0.00    53.8 vs  60.0
PREMIERENE.NS 2025-03-25 to 2026-11-16  1.61    -2.7  -0.12  -33.1    -6.4  -0.19  -39.3     4.1   0.30  -15.6    -3.4  -0.15    0.52    36.4 vs  49.0
HDFCBANK.NS   2021-11-16 to 2026-11-16   4.9     0.9   0.09  -18.7     2.6   0.12  -31.7     7.3   0.52  -16.5    -0.7  -0.07    0.72    72.5 vs  52.1
```

From `signals_eval.json`, same run:

| ticker | time in market | trades | costs paid (₹, on ₹1,00,000) | random Sharpe 5th–95th pct | down 20 days after SELL vs base |
|---|---|---|---|---|---|
| COFORGE.NS | 47% | 12 | 4,384 | −0.26 to 0.81 | 45.9% vs 41.0% |
| DIXON.NS | 43% | 11 | 4,851 | −0.43 to 1.54 | 48.7% vs 43.2% |
| SBIN.NS | 51% | 21 | 6,124 | −0.04 to 0.99 | 34.6% vs 40.1% |
| PREMIERENE.NS | 39% | 4 | 1,378 | −0.98 to 0.69 | 60.3% vs 51.0% |
| HDFCBANK.NS | 21% | 20 | 6,800 | −0.67 to 0.56 | 48.7% vs 47.9% |

Buy-and-hold paid ₹169 in costs on each stock (one purchase).

## What it means (inferred from the numbers above)

- **Return.** The signal's CAGR was below buy-and-hold on all four full-history
  stocks: by 1.7 points (HDFC Bank) up to 22.7 points (SBI). On SBI it lost
  money while the stock compounded at 19.7% a year.
- **Drawdown.** Lower than buy-and-hold on COFORGE, DIXON and HDFCBANK, higher
  on SBIN. Mostly a consequence of being out of the market 49–79% of the time:
  any rule that is often flat has smaller drawdowns.
- **Timing.**
  - Against its own shifted copies, the signal ranked at the 39th, 76th, 0th
    and 72nd percentile on the four full-history stocks.
  - None reaches the 95th percentile, the usual bar for "unlikely to be luck".
  - SBIN at the 0th percentile means every one of the 200 random re-timings did
    better.
- **Hit rate.**
  - After a BUY, HDFC Bank was higher 20 days later 72.5% of the time against
    a 52.1% base rate, and COFORGE 64.7% against 59.1%.
  - DIXON was level with its base rate and SBI below it.
  - Overlapping 20-day windows make these counts far less independent than
    their size suggests. They are not significance tests.
- **Limited history (PREMIERENE.NS).**
  - Only 1.6 years could be evaluated, all on limited-history settings.
  - The signal lost less than buy-and-hold (−2.7% against −6.4% a year), in a
    falling stock where being out half the time helps by itself.
  - That is not evidence of skill. 1.6 years is too short to tell.

## Weaknesses found while checking today's signals

- **A narrow vote can look confident.**
  - PREMIERENE.NS shows BUY at +0.89 because only 3 of its 11 eligible rules
    had a positive training Sharpe.
  - Two reversion rules carry 94% of the weight.
  - Its confidence (0.22) is cut by the limited-history factor, but not for
    the narrowness of the vote.
- **The regime direction lags a V-shaped turn.**
  - SBIN.NS is labelled `trending_down`, with ADX 42, +DI 38 and −DI 6, because
    the 50-day average's 20-day slope is still negative after a rebound.
  - The DI lines already point up.
- **Not fixed here.** Changing either now would be adjusting the method after
  seeing its results, so both are recorded instead. A fix would need a fresh
  evaluation window, or at least a statement that it was made after this run.

## Not measured

- No comparison on US stocks.
- No other thresholds or windows: deliberately, to avoid fitting them to this
  sample.
- No significance test beyond the random-timing percentile.
