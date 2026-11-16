---
key: macd_cross
name: MACD signal crossover
kind: equity
regime: trending
code: market/strategies.py#macd_cross
---
# MACD signal crossover

## Summary
MACD is the gap between a 12-day and a 26-day exponential average; its own
9-day average is the "signal line". The strategy holds while MACD is above
its signal line, meaning the short-term trend is gaining on the longer one.

## Rules
Entry: MACD line above the signal line. Exit: below it.

## Regime
Short to medium swings within a trend.

## When it fails
It measures the change in momentum and so turns very often. In a flat market,
small wiggles cross the signal line again and again. It can also turn bearish
in the middle of a strong uptrend just because the rise slowed.

## Risk
High turnover, so costs matter. Many small losses that a trend has to pay
back.

## Parameters
`fast` 12, `slow` 26, `signal` 9: Gerald Appel's original settings.

## Code
`macd_cross` in `market/strategies.py`.
