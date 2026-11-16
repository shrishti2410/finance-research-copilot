---
key: trend_alignment
name: Moving-average alignment
kind: equity
regime: trending
code: market/strategies.py#trend_alignment
---
# Moving-average alignment

## Summary
Hold only when the short, medium and long trends all agree: the close is
above the 20-day average, which is above the 50-day, which is above the
200-day. Traders sometimes call this a "stacked" or "aligned" trend.

## Rules
Long while close > SMA 20 > SMA 50 > SMA 200. Flat as soon as any one of
those breaks.

## Regime
Clean, established uptrends.

## When it fails
Its condition is strict, so it is often flat and misses the early part of a
trend. A single bad week breaks the shortest link and forces an exit that a
slower rule would sit through.

## Risk
Long or flat, with short holding periods relative to the trend, so costs add
up. It is in the market much less than buy and hold.

## Parameters
`short` 20, `medium` 50, `long` 200.

## Code
`trend_alignment` in `market/strategies.py`.
