---
key: atr_trailing_stop
name: Trend with an ATR trailing stop
kind: equity
regime: trending
code: market/strategies.py#atr_trailing_stop
---
# Trend with an ATR trailing stop

## Summary
Enter when the price is above its 50-day average. Then trail a stop three
Average True Ranges below the highest close since entry (a "chandelier
exit"). ATR measures a stock's typical daily range, so the stop is wider for
a volatile stock and tighter for a calm one.

## Rules
Entry: close above the 50-day SMA. Exit: close more than 3 x ATR(22) below the
highest close since entry. After an exit it re-enters at the next close above
the average.

## Regime
Trends with pullbacks: the volatility-scaled stop gives a trend room to
breathe.

## When it fails
A slow grind down that never sets off the stop but erodes value, or a quick
re-entry after the exit straight into a second leg down.

## Risk
The worst case on a trade is roughly three ATRs from the peak plus any gap.
That is a known size at entry, which most rules here do not offer.

## Parameters
`trend` 50, `atr_period` 22, `multiple` 3.0.

## Code
`atr_trailing_stop` in `market/strategies.py`.
