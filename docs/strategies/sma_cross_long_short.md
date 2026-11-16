---
key: sma_cross_long_short
name: Moving-average crossover, long and short
kind: equity
regime: trending
code: market/strategies.py#sma_cross_long_short
---
# Moving-average crossover, long and short

## Summary
The golden-cross rule, but instead of going flat below the line it goes
short, betting on the decline. It is always in the market once both averages
exist.

## Rules
Long while the 50-day SMA is above the 200-day SMA; short while it is below.

## Regime
Long trends in both directions, such as a multi-year bear market.

## When it fails
Sideways markets, where the short side loses as the price bounces. In a
market that mostly rises, short positions are a steady drag.

## Risk
A short's loss has no ceiling, and shorts pay borrow costs this engine does
not model. **On NSE and BSE it cannot run as written:** an equity delivery
position cannot be held short overnight in the cash segment, so the Indian
cost model turns every short into flat. For an Indian stock this card's
backtest is the long-or-flat golden cross.

## Parameters
`fast` 50, `slow` 200.

## Code
`sma_cross_long_short` in `market/strategies.py`.
