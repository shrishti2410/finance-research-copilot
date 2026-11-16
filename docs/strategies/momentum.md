---
key: momentum
name: Time-series momentum (12-1)
kind: equity
regime: trending
code: market/strategies.py#momentum
---
# Time-series momentum (12-1)

## Summary
Hold the stock if it went up over the last year, measured from 12 months ago
to 1 month ago. The latest month is skipped because very short-term returns
tend to reverse. Momentum is one of the most studied effects in finance:
assets that rose have, on average, kept rising for a while.

## Rules
Entry: the return from 252 to 21 trading days ago is positive. Exit: it is
negative. Rechecked every day.

## Regime
Persistent trends. It needs a year of history before its first signal.

## When it fails
Sharp reversals ("momentum crashes"), such as the rebound after a bear market:
the rule is still out, or still in, because of what happened up to a year ago.

## Risk
Slow to react in both directions. A stock with less than about 13 months of
history gives it no signal at all.

## Parameters
`lookback` 252 and `skip` 21 trading days.

## Code
`momentum` in `market/strategies.py`.
