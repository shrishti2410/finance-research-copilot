# The double vote: drop the duplicate rule (adopted), cluster votes (rejected)

The pre-registered comparison of [RULE_DEDUP_RULES.md](RULE_DEDUP_RULES.md),
applied as written. The rules were committed in `d6a1de1` (18:41 UTC on
2026-11-17; the file's "2026-11-18" is the local date in India) and the code in
`645bfbd`, both before this run.

## Verdicts

| rule | test | result |
|---|---|---|
| 1. variant A: drop `zscore_reversion` | the two rules agree on ≥ 99% of days, and median Sharpe falls by no more than 0.05 | **adopted (proposed).** Agreement **100%** of days, pooled over the 15 stocks: they are one rule. Median Sharpe 0.436 → 0.459 (+0.02), which is **flat**: within one run's noise, not an improvement. |
| 2. variant B: one vote per correlated cluster | +0.10 median Sharpe against A, higher on ≥ 10/15 and ≥ 7/10 hold-out | **rejected.** Median Sharpe 0.171, −0.29 against A; higher on 5 of 15, and on 1 of 10 hold-out stocks. |

**What adopting A changes.** Positions change on 0 to 169 days per stock out
of 1,236, even though the two rules always agree. Removing one copy
re-normalises every other rule's share of the vote. The per-stock Sharpe moves
in both directions (higher on 9 of 15). The verdicts of
[QUANT_HONESTY.md](QUANT_HONESTY.md) do not change: still no edge over
buy-and-hold (median 0.65).

**Why B fails (inferred, not tested separately).** Collapsing each cluster to
its best-Sharpe member keeps the rule that looked best over the last two
years. That is the same move as the walk-forward "best rule", which scored
0.22 in QUANT_HONESTY.md: it chases the recent winner. Clustering also cut
exposure (39% against 46%) and raised turnover.

## Medians across the 15 stocks

Net of costs, 5 years to the close of 2026-11-13.

| variant | Sharpe | CAGR | max drawdown | exposure | turnover / yr | cost drag |
|---|---:|---:|---:|---:|---:|---:|
| production (16 rules) | 0.436 | 4.4% | −31.0% | 44.6% | 6.1 | 1.07 |
| **A: duplicate dropped (15 rules)** | **0.459** | 5.6% | −29.3% | 46.1% | 5.5 | 0.94 |
| B: one vote per cluster | 0.171 | 1.2% | −30.0% | 39.0% | 7.1 | 1.27 |

## Per stock (Sharpe)

The **bold** names are the 10 hold-out stocks.

| stock | production | A | B | days A changed |
|---|---:|---:|---:|---:|
| COFORGE | 0.03 | 0.09 | 0.15 | 1 |
| DIXON | 1.08 | 1.10 | 0.53 | 3 |
| SBIN | −0.26 | −0.11 | −0.05 | 115 |
| PREMIERENE | 0.11 | −0.15 | −0.03 | 8 (of 405) |
| HDFCBANK | −0.22 | −0.35 | −0.34 | 79 |
| **RELIANCE** | −0.25 | −0.15 | −0.46 | 119 |
| **TCS** | −0.83 | −0.52 | −0.74 | 62 |
| **ICICIBANK** | 0.75 | 0.68 | 0.17 | 126 |
| **ITC** | 0.44 | 0.53 | 0.18 | 59 |
| **LT** | 0.69 | 0.70 | 0.46 | 26 |
| **SUNPHARMA** | 0.49 | 0.46 | 0.55 | 169 |
| **VOLTAS** | −0.13 | −0.28 | −0.40 | 67 |
| **TVSMOTOR** | 0.94 | 0.81 | 0.68 | 51 |
| **CUMMINSIND** | 1.19 | 1.37 | 1.05 | 65 |
| **PERSISTENT** | 0.57 | 0.70 | 0.57 | 79 |

Every stock's window is 1,236 days except PREMIERENE's, which is 405 (a recent
listing).

## Proposed next step (not done here)

Variant A in the product, on its own branch with the user's approval. That
branch:
- removes `zscore_reversion` from the voters (`market/signals.py`
  candidates) and keeps its strategy card, marked as the same rule as
  Bollinger reversion;
- changes the coverage text from "of 16 rules" to "of 15 rules" (NSE) and from
  17 to 16 (US), and updates the tests that pin the voter count;
- re-runs the precompute, since the signal code changes.

## How it was run

| | |
|---|---|
| command | `python scripts/rule_dedup.py --out benchmarks/results/2026-11-18-rule-dedup` |
| commit | `645bfbd` (`bench/rule-dedup`); rules `d6a1de1` |
| date | finished 2026-11-17 18:43:18 UTC, 17.6 s |
| machine | Windows 11 laptop, Python 3.11.6, plugged in; standby not blocked (an 18 s run during an active session) |
| data | the local price cache (`yfinance`), the same frozen window as QUANT_HONESTY.md |
| model | none |

**Check against the earlier run:** production's median Sharpe here, 0.436,
matches the 0.44 of QUANT_HONESTY.md.

Raw output, gitignored: `benchmarks/results/2026-11-18-rule-dedup/`
(`rule_dedup.json`, `run.log`).
