---
key: sma_cross
name: Moving-average crossover (golden cross)
kind: equity
regime: trending
code: market/strategies.py#sma_cross
---
# Moving-average crossover (golden cross)

## Summary
Hold the stock while its 50-day average price is above its 200-day average.
The fast average crossing above the slow one (a "golden cross") says the
recent trend has turned up; crossing below (a "death cross") says it has
turned down.

## Rules
Entry: the 50-day SMA closes above the 200-day SMA. Exit: it closes below.
Decided at the close, traded at the next open.

## Regime
Long, persistent trends lasting many months. It trades rarely, a few times a
year at most, so costs barely matter.

## When it fails
In a sideways market the averages cross back and forth and every trade loses a
little ("whipsaw"). It is also late by design: both averages lag, so it gives
back a large part of a move before it exits, and buys after much of the
recovery.

## Risk
Out of the market during most crashes that build slowly, but a sharp gap down
is taken in full before the averages react. Long or flat only.

## Parameters
`fast` 50 and `slow` 200 days, the common textbook values. Shorter windows
react faster and whipsaw more.

## Code
`sma_cross` in `market/strategies.py`.
