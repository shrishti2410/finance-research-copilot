---
key: bollinger_breakout
name: Bollinger breakout
kind: equity
regime: trending
code: market/strategies.py#bollinger_breakout
---
# Bollinger breakout

## Summary
The opposite reading of the same bands: a close above the upper band is a
move strong enough to start a trend. Buy it, and sell when the price falls
back below the 20-day average.

## Rules
Entry: close above the upper band (20 days, 2 standard deviations). Exit:
close below the middle band.

## Regime
The start of a trend out of a quiet period, especially after the bands have
narrowed (a "squeeze").

## When it fails
False breakouts: the price pokes above the band and falls straight back,
costing a round trip. Common in ranges and after one-day news spikes.

## Risk
It buys after a strong up day, so the entry is often at a short-term high.

## Parameters
`period` 20, `width` 2.0.

## Code
`bollinger_breakout` in `market/strategies.py`.
