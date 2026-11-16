---
key: buy_and_hold
name: Buy and hold
kind: equity
regime: any
code: market/strategies.py#buy_and_hold
---
# Buy and hold

## Summary
Own the stock every day and do nothing else. It is the yardstick: a timing
strategy earns its complexity only if it beats this after costs, or matches it
with clearly smaller losses on the way.

## Rules
Entry: buy on the first bar of the test window. Exit: none; the position is
marked to the last close. One purchase, so costs are paid once.

## Regime
Works in any regime in the sense that it never misses a rally. It does best in
long rising markets, where every timing rule risks being out at the wrong time.

## When it fails
In a long bear market or a single-stock collapse it rides the whole fall. It
has no idea of risk: a stock that halves stays held.

## Risk
Full exposure all the time, so its drawdown is the stock's drawdown. For one
stock that can be far deeper than an index; a single company can fail outright.

## Parameters
None.

## Code
`buy_and_hold` in `market/strategies.py`. Every backtest also reports it as the
benchmark on the same prices and costs.
