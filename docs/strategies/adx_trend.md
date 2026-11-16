---
key: adx_trend
name: ADX trend strength
kind: equity
regime: trending
code: market/strategies.py#adx_trend
---
# ADX trend strength

## Summary
Welles Wilder's ADX measures how strongly a price is trending, from 0 to 100,
without saying which way. The +DI and -DI lines say which way. The strategy
holds while ADX shows a strong trend and +DI shows it is up.

## Rules
Long while ADX(14) is above 25 and +DI is above -DI. Flat otherwise.

## Regime
Strong trends only. It sits out the quiet periods where crossover rules
whipsaw.

## When it fails
ADX is slow and peaks late. By the time it is above 25 a good part of the
move may be over, and it often stays high into the first part of a reversal.

## Risk
Long or flat. It can miss the first and best leg of a trend while ADX builds
up.

## Parameters
`period` 14, `threshold` 25 (Wilder's own reading of a "trending" market).

## Code
`adx_trend` in `market/strategies.py`; the indicator is
`market.indicators.adx`. The signal engine also uses ADX to classify the
market regime.
