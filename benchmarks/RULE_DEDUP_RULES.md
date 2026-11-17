# Stopping the double vote: the variants and adoption rules, before the run

Written and committed on 2026-11-18, before the variants below were run on
real prices. Results go in `benchmarks/RULE_DEDUP.md`.

## The problem

In [QUANT_HONESTY.md](QUANT_HONESTY.md), `bollinger_reversion` and
`zscore_reversion` had an average return correlation of **1.00** across the 15
NSE stocks. By definition they are one rule:
- Bollinger reversion buys a close below the 20-day mean minus 2 standard
  deviations, and sells back at the mean.
- Z-score reversion buys a 20-day z-score below −2, and sells at 0.

So that view is counted twice in the vote. Eight other pairs of rules
correlate above 0.80.

## The variants

All are walk-forward on the production signal's own monthly refits, over the
same data, window and costs as QUANT_HONESTY_RULES.md: the 15 NSE stocks, 5
years to the close of 2026-11-13, Indian delivery costs.

| variant | change |
|---|---|
| **production** | the signal as it is (16 rules) |
| **A: drop the duplicate** | `zscore_reversion` takes no part in the vote; `bollinger_reversion`, which has the strategy card, stays. Everything else unchanged. |
| **B: cluster correlated rules** | at each refit, rules whose daily net returns over **that refit's own training window** correlate above 0.80 form a cluster. Rules are taken in descending order of trailing Sharpe; each unassigned rule starts a cluster and takes in every unassigned rule above 0.80 with it. Each cluster votes once, with its leader's weight; the other members weigh 0. Only past data is used, so there is no look-ahead. |

The metrics are the same as in QUANT_HONESTY_RULES.md.

## Adoption rules (decided now)

1. **Variant A is a correction, not a tuning.** It is adopted if both hold:
   - (a) the two rules hold the same position on at least 99% of the days in
     the window, pooled over the 15 stocks. This checks that they really are
     the same rule.
   - (b) the median Sharpe across the 15 stocks does not fall by more than
     0.05 against production. This guards against an unexpected side effect.

   Better results are **not** required: counting one rule twice is a defect
   whatever it does to the numbers.
2. **Variant B is a model change**, judged by QUANT_HONESTY_RULES.md rule 1
   against the signal that would ship: variant A if A is adopted, production
   otherwise. It is adopted only if all three hold:
   - its median Sharpe is higher by at least 0.10;
   - its Sharpe is higher on at least 10 of the 15 stocks;
   - its Sharpe is higher on at least 7 of the 10 hold-out stocks.
3. **Before and after are recorded whatever the outcome.**
4. **Adoption here is a proposal.** Changing the product is its own branch with
   the user's approval. That branch would also update the coverage text
   ("12 of 16 rules" would count 15 rules), the strategy-card list, and the
   tests that pin 16 voters.

Nothing in these rules may change after the first run.
