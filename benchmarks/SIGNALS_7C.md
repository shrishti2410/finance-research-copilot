# The signal engine out of sample: five NSE stocks, 2021-11-16 to 2026-11-16

> **Later finding (2026-11-17,
> [QUANT_HONESTY.md](QUANT_HONESTY.md)).** The smaller drawdowns reported here are
> what the signal's lower exposure gives anyway. Against buy-and-hold held at
> the signal's own exposure, its drawdown was smaller on only 2 of 15 stocks.
> The measurements here stand; the reading of them as a benefit does not.

Step 7C, phase 3. The method is in `market/signals.py`, the evaluation in
`market/signal_eval.py`, the design in `docs/SIGNALS.md`.

## Current numbers (calendar refit, commit `eac9d0c`, 2026-11-16 19:10 UTC)

**The finding: no return edge; lower drawdowns, mostly from time out of the
market.** It holds on 15 stocks and on every one of 20 refit schedules except
for one stock (DIXON.NS), which is about what chance gives in 15 tries.

**Why these numbers replace the first ones.**
- The first measurements (kept under "History" below) refitted the weights
  every 21 bars counted from the first signal, so where the history started
  decided the refit days.
- A rerun six hours later fetched one day less history (the window rolls with
  the date). Every refit moved, and COFORGE.NS's signal CAGR went from 3.5% to
  4.4%.
- Refits are now on the first trading day of each month (`eac9d0c`), and a
  test checks that the start of the history no longer moves later signals.
- The same rerun also caught Yahoo returning NSE histories without the latest
  session. The cache now keeps bars a later fetch omits (`f74ef6d`).

**How it was run:**
- commands, all on `eac9d0c`, from 2026-11-16 19:10 to 19:16 UTC:
  - `python scripts/evaluate_signals.py --out benchmarks/results/2026-11-17-calendar-refit/watchlist`
  - the same with the ten hold-out tickers, into `.../holdout`
  - `python scripts/refit_day_spread.py --out benchmarks/results/2026-11-17-calendar-refit/spread`
- machine: the same laptop as below, with standby blocked.
- data: the price cache as loaded on 2026-11-16 (bars to 2026-11-16).
- the raw output is in that gitignored folder.

### Watchlist, refit on the first trading day of the month

Window 2021-11-16 to 2026-11-16 (PREMIERENE.NS from 2025-03-25), Indian
delivery costs. Sharpe is daily, no risk-free rate; Calmar = CAGR ÷ |max DD|.
"pctl" is the share of 200 random re-timings with a lower Sharpe.

| ticker | | Sharpe | Sortino | Calmar | time in market | CAGR | max DD | pctl |
|---|---|---|---|---|---|---|---|---|
| COFORGE.NS | signal | 0.03 | 0.04 | −0.04 | 46.1% | −1.7% | −37.0% | 0.20 |
| | buy & hold | 0.53 | 0.77 | 0.28 | 100% | 13.3% | −46.7% | |
| DIXON.NS | signal | 1.08 | 1.63 | 1.02 | 44.6% | 26.8% | −26.3% | 0.89 |
| | buy & hold | 0.65 | 0.95 | 0.36 | 100% | 19.1% | −53.7% | |
| SBIN.NS | signal | −0.22 | −0.29 | −0.16 | 49.0% | −5.3% | −34.0% | 0.00 |
| | buy & hold | 0.86 | 1.23 | 0.82 | 100% | 19.7% | −23.9% | |
| PREMIERENE.NS (1.6 years) | signal | 0.11 | 0.15 | −0.00 | 38.0% | −0.1% | −33.1% | 0.64 |
| | buy & hold | −0.02 | −0.03 | −0.16 | 100% | −6.4% | −39.3% | |
| HDFCBANK.NS | signal | −0.22 | −0.30 | −0.09 | 20.6% | −2.1% | −24.1% | 0.27 |
| | buy & hold | 0.23 | 0.32 | 0.08 | 100% | 2.6% | −31.7% | |
| Nifty 50 (2021-11-16 on) | index | 0.57 | 0.81 | 0.44 | 100% | 7.3% | −16.5% | |

**Return and Sharpe.**
- Signal below buy-and-hold on CAGR and on all three ratios for COFORGE,
  SBIN and HDFCBANK.
- Above on all of them for DIXON.
- Above for PREMIERENE, where buy-and-hold lost 6.4% a year over 1.6 years and
  the signal, out 62% of the time, roughly broke even.

**Timing.** No stock reached the 95th percentile against random re-timings.
DIXON came closest at 0.89, so the verdict is still "no edge"
(`backtest_verdict` needs 0.95).

**Drawdown.** Smaller than buy-and-hold on four of five; SBIN is the
exception.

### Hold-out: ten stocks never used to build anything

- CAGR below buy-and-hold on **10 of 10**.
- Daily Sharpe below on **10 of 10**.
- Max drawdown smaller on 7 of 10.
- Time in market 21–64%.
- Random-timing percentile 0.03 to 0.75.

The per-stock figures are in `holdout/signals_eval.json`.

### How much the refit day matters (measured)

The evaluation was run once for each refit day, the 1st to the 20th trading
day of the month: 20 schedules × 15 stocks. Buy-and-hold does not depend on
the schedule.

| ticker | signal Sharpe min / median / max | b&h Sharpe | signal CAGR min / median / max | b&h CAGR | schedules beating b&h on CAGR and Sharpe |
|---|---|---|---|---|---|
| COFORGE.NS | −0.01 / 0.20 / 0.30 | 0.53 | −2.4% / 2.0% / 4.3% | 13.3% | 0 of 20 |
| DIXON.NS | 0.79 / 0.95 / 1.10 | 0.65 | 17.4% / 22.8% / 28.1% | 19.1% | 18 of 20 |
| SBIN.NS | −0.24 / −0.04 / 0.16 | 0.86 | −5.6% / −2.4% / 1.2% | 19.7% | 0 of 20 |
| PREMIERENE.NS | −0.85 / −0.26 / 0.18 | −0.02 | −17.4% / −6.8% / 1.6% | −6.4% | 5 of 20 |
| HDFCBANK.NS | −0.41 / −0.01 / 0.18 | 0.23 | −3.7% / −0.4% / 1.0% | 2.6% | 0 of 20 |
| RELIANCE.NS | −0.27 / −0.17 / −0.10 | 0.27 | −3.5% / −2.4% / −1.7% | 3.5% | 0 of 20 |
| TCS.NS | −1.00 / −0.70 / −0.34 | −0.11 | −9.7% / −6.8% / −4.0% | −4.9% | 0 of 20 |
| ICICIBANK.NS | 0.58 / 0.71 / 0.85 | 0.89 | 7.9% / 10.0% / 12.4% | 17.0% | 0 of 20 |
| ITC.NS | 0.27 / 0.47 / 0.74 | 0.57 | 2.6% / 5.7% / 9.6% | 9.9% | 0 of 20 |
| LT.NS | 0.58 / 0.70 / 0.78 | 0.79 | 9.2% / 11.8% / 13.7% | 17.8% | 0 of 20 |
| SUNPHARMA.NS | 0.46 / 0.53 / 0.59 | 1.02 | 5.7% / 6.7% / 7.7% | 20.1% | 0 of 20 |
| VOLTAS.NS | −0.37 / −0.13 / 0.04 | 0.12 | −8.3% / −3.8% / −0.8% | −0.9% | 0 of 20 |
| TVSMOTOR.NS | 0.91 / 0.99 / 1.08 | 1.50 | 18.1% / 20.3% / 22.5% | 44.9% | 0 of 20 |
| CUMMINSIND.NS | 1.02 / 1.22 / 1.34 | 1.24 | 21.7% / 27.5% / 30.6% | 38.4% | 0 of 20 |
| PERSISTENT.NS | 0.46 / 0.61 / 0.79 | 0.80 | 7.6% / 11.1% / 15.2% | 24.1% | 0 of 20 |

What the spread means:
- **Single runs are noisy.** The scheduling choice alone moves a five-year
  CAGR by up to 11 points for a stock with full history (DIXON: 17.4% to
  28.1%; COFORGE: −2.4% to 4.3%), and by 19 points for PREMIERENE over 1.6
  years. A difference between
  two single runs smaller than this is not a finding.
- **The finding holds across schedules.** On 13 of 15 stocks no schedule
  beats buy-and-hold on both CAGR and Sharpe. PREMIERENE does on 5 of 20,
  where buy-and-hold itself lost money.
- **DIXON is the one exception.** It beats buy-and-hold on 18 of 20 schedules.
  - With 15 stocks tested, one standing out is roughly what chance alone gives.
  - Its timing still sits below the 95% random-timing bar.
  - It is not evidence of an edge without a test on new data.

**Today's signals** (2026-11-16 close, `eac9d0c`), each with its coverage:

| ticker | signal | score | coverage | confidence | note |
|---|---|---|---|---|---|
| COFORGE.NS | BUY | +0.684 | 15 of 16 rules | 0.684 | |
| DIXON.NS | BUY | +0.317 | 6 of 16 rules | 0.317 | narrow vote |
| SBIN.NS | BUY | +0.437 | 12 of 16 rules | 0.437 | |
| PREMIERENE.NS | BUY | +0.315 | 3 of 11 rules | 0.079 | limited history, narrow vote |
| HDFCBANK.NS | SELL | −0.670 | 5 of 16 rules | 0.312 (uncapped 0.670) | narrow vote |

---

## History: the first measurements, on the old refit schedule

Everything below was measured with refits every 21 bars from the first signal
(commits `dd2dbb8` and `afe350b`). It is kept as measured and superseded by
the section above; the refit-day spread shows how much of the difference is
scheduling noise.

**The finding at the time: no return edge; lower drawdowns, mostly from time
out of the market.**
- **Return.** The signal's CAGR was below buy-and-hold on all four stocks with
  five years of history. On risk-adjusted terms its daily Sharpe was higher on
  one stock (DIXON) and lower on the other three.
- **Timing.** On none of the five stocks did it beat its own randomly re-timed
  copies convincingly.
- **Drawdown.** It was in the market 21–51% of the time. Its maximum drawdown
  was smaller on three of the four, which is what being out of the market that
  much does by itself.
- **Hold-out confirmation.** The same engine, unchanged, on ten NSE stocks it
  had never seen (`benchmarks/REGIME_RULE_CHECK.md`, the `slope` run) gives the
  same picture:
  - CAGR below buy-and-hold on 10 of 10;
  - daily Sharpe above buy-and-hold on 1 of 10;
  - drawdown smaller on 7 of 10.

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

## Risk-adjusted, every stock (measured)

Added on request after the first run. The figures come from
`python scripts/evaluate_signals.py --regime-rule slope --out
benchmarks/results/2026-11-16-regime-check/watchlist-slope`, run on commit
`afe350b` on 2026-11-16 at 17:47 UTC, same machine, standby blocked.

- **Same engine as above:** `slope` is the default rule, and nothing between
  `dd2dbb8` and `afe350b` changes a position. The CAGR and drawdown columns
  match the table above.
- **Definitions** (all without a risk-free rate):
  - **Sharpe:** mean ÷ standard deviation of daily returns × √252. This is the
    standard form, so it differs from the CAGR ÷ volatility "Sharpe" in the
    first table.
  - **Sortino:** the same, with the downside deviation over all days.
  - **Calmar:** CAGR ÷ |max drawdown|.

| ticker | | Sharpe | Sortino | Calmar | time in market | CAGR | max DD |
|---|---|---|---|---|---|---|---|
| COFORGE.NS | signal | 0.27 | 0.39 | 0.09 | 46.7% | 3.5% | −37.0% |
| | buy & hold | 0.53 | 0.77 | 0.28 | 100% | 13.3% | −46.7% |
| DIXON.NS | signal | 0.74 | 1.09 | 0.58 | 42.6% | 16.1% | −27.9% |
| | buy & hold | 0.65 | 0.95 | 0.36 | 100% | 19.1% | −53.7% |
| SBIN.NS | signal | −0.07 | −0.10 | −0.10 | 50.5% | −3.0% | −29.9% |
| | buy & hold | 0.86 | 1.23 | 0.82 | 100% | 19.7% | −23.9% |
| PREMIERENE.NS (1.6 years) | signal | −0.01 | −0.02 | −0.08 | 38.5% | −2.7% | −33.1% |
| | buy & hold | −0.02 | −0.03 | −0.16 | 100% | −6.4% | −39.3% |
| HDFCBANK.NS | signal | 0.14 | 0.20 | 0.05 | 21.4% | 0.9% | −18.7% |
| | buy & hold | 0.23 | 0.32 | 0.08 | 100% | 2.6% | −31.7% |
| Nifty 50 (2021-11-16 on) | index | 0.57 | 0.81 | 0.44 | 100% | 7.3% | −16.5% |

The Nifty row covers the five-year window. Over PREMIERENE's 1.6-year window
the Nifty had Sharpe 0.37, Sortino 0.54, Calmar 0.27, CAGR 4.1% and max
drawdown −15.6%.

- **Signal ahead of buy-and-hold:** on all three ratios for DIXON, and
  marginally for PREMIERENE, where both are negative over 1.6 years.
- **Signal behind:** on all three ratios for COFORGE, SBIN and HDFCBANK.
- **Against the Nifty:** the index beat the signal on Sharpe for every stock
  except DIXON.

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

What happened to each (2026-11-16, after this record):

- **Narrow vote: shown, not hidden** (`5f7e7e5`, display only).
  - Every signal now states its coverage, e.g. "3 of 11 rules".
  - Confidence is capped at the coverage fraction, and fewer than half the
    rules counting adds a warning.
  - The decision rule and every position are unchanged, so nothing above
    moves.
  - Today the cap binds on HDFCBANK.NS: SELL on 6 of 16 rules, confidence
    0.666 capped to 0.375.
  - For PREMIERENE.NS it does not bind. Its confidence (0.22) is already below
    3/11 = 0.27 because of the limited-history factor; the narrow-vote warning
    still shows.
- **Regime lag: tested out of sample and not adopted**
  (`benchmarks/REGIME_RULE_CHECK.md`).
  - The proposed rule raised the signal's Sharpe on 3 of 10 hold-out stocks.
  - The median change was 0.000.
  - The pre-registered bar was 6 of 10 and a positive median.

## Not measured

- No comparison on US stocks.
- No other thresholds or windows: deliberately, to avoid fitting them to this
  sample.
- No significance test beyond the random-timing percentile.
