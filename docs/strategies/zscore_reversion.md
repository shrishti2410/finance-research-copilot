---
key: zscore_reversion
name: Z-score mean reversion
kind: equity
regime: ranging
code: market/strategies.py#zscore_reversion
---
# Z-score mean reversion

## Summary
The z-score is how many standard deviations today's close sits from its
20-day average. The strategy buys a close two standard deviations below the
average and sells when the price returns to it: the Bollinger reversion rule
stated as a number.

## Rules
Entry: z-score below -2. Exit: z-score above 0.

## Regime
Ranges, and stable stocks that revert to an average.

## When it fails
Trending declines, and stocks repriced by news. A two-standard-deviation drop
is not rare, because daily returns have fat tails.

## Risk
As with every reversion rule: frequent small wins, occasional large losses,
and no stop.

## Parameters
`period` 20, `entry` -2.0, `exit` 0.0.

## Code
`zscore_reversion` in `market/strategies.py`.
