---
key: price_above_sma
name: Trend filter (price above its 200-day average)
kind: equity
regime: trending
code: market/strategies.py#price_above_sma
---
# Trend filter (price above its 200-day average)

## Summary
Hold the stock while it closes above its 200-day average and step aside
while it is below. The most common "is this a long-term uptrend?" test, used
on its own or as a filter for other rules.

## Rules
Entry: close above the 200-day SMA. Exit: close below it.

## Regime
Long uptrends and long downtrends, where the price stays well away from the
average.

## When it fails
When the price hovers near the average, each small cross is a trade. That is
common after a long rise flattens out. It also exits only after the price has
already fallen to the average, which can be well off the peak.

## Risk
Long or flat. It avoids the middle of long declines but not their first leg.

## Parameters
`period` 200 trading days, roughly ten months.

## Code
`price_above_sma` in `market/strategies.py`.
