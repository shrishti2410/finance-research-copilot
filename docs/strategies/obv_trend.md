---
key: obv_trend
name: On-balance volume trend
kind: equity
regime: trending
code: market/strategies.py#obv_trend
---
# On-balance volume trend

## Summary
On-balance volume adds a day's volume when the stock closes up and subtracts
it when it closes down. A rising OBV means more shares change hands on up
days. The strategy holds while both price and OBV are above their 50-day
averages, so the rise is backed by volume.

## Rules
Long while OBV is above its 50-day SMA and the close is above its 50-day SMA.

## Regime
Uptrends with broad participation.

## When it fails
Volume data is noisy: block deals, index rebalancing and results days add
spikes that move OBV with no change in trend. A single huge-volume day can
dominate OBV for months.

## Risk
Long or flat. OBV adds little when volume barely varies.

## Parameters
`period` 50.

## Code
`obv_trend` in `market/strategies.py`; the indicator is
`market.indicators.obv`.
