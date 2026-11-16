---
key: ema_cross
name: EMA crossover
kind: equity
regime: trending
code: market/strategies.py#ema_cross
---
# EMA crossover

## Summary
The same idea as the golden cross, with exponential averages over much
shorter windows (12 and 26 days). Exponential averages weight recent prices
more, so it reacts within weeks rather than months.

## Rules
Entry: the 12-day EMA is above the 26-day EMA. Exit: it falls below.

## Regime
Medium-term trends of a few weeks to a few months.

## When it fails
Choppy markets: it flips far more often than the 50/200 cross and pays costs
each time. With Indian delivery charges (about 0.24% a round trip) a dozen
round trips a year cost around 3% before any loss on the trades themselves.

## Risk
Short holding periods mean many small losses in a range and a few large gains
in a trend; the win rate is often below half.

## Parameters
`fast` 12, `slow` 26: the same pair MACD uses.

## Code
`ema_cross` in `market/strategies.py`.
