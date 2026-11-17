---
key: keltner_breakout
name: Keltner channel breakout
kind: equity
regime: trending
code: market/strategies.py#keltner_breakout
---
# Keltner channel breakout

## Summary
A Keltner channel is a 20-day exponential average with bands two Average
True Ranges above and below. A close above the upper band is an unusually
strong move for this stock. The strategy buys it and rides the move until the
price falls back below the average.

## Rules
Entry: close above EMA(20) + 2 x ATR(20). Exit: close below EMA(20).

## Regime
Trend starts and strong trends. Because ATR uses the daily high-low range,
the bands react to gaps and wide days that a close-only band can miss.

## When it fails
Spikes that reverse at once, and ranges. Like every breakout rule it buys
near a short-term high.

## Risk
The exit at the average can be several ATRs below the entry after a fast move.

## Parameters
`period` 20, `multiple` 2.0.

## Code
`keltner_breakout` in `market/strategies.py`; the channel is
`market.indicators.keltner`.
