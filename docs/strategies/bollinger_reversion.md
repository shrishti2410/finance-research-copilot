---
key: bollinger_reversion
name: Bollinger mean reversion
kind: equity
regime: ranging
code: market/strategies.py#bollinger_reversion
---
# Bollinger mean reversion

## Summary
Bollinger bands sit two standard deviations above and below a 20-day average.
A close below the lower band is an unusually large drop for this stock's
recent volatility. The strategy buys it and sells once the price is back at
the average.

## Rules
Entry: close below the lower band (20 days, 2 standard deviations). Exit:
close above the middle band (the 20-day average).

## Regime
Quiet, sideways markets where moves away from the average fade.

## When it fails
At the start of a decline, or after news that resets what the company is
worth. The bands widen as volatility rises, but the price can "walk down" the
lower band for weeks.

## Risk
Buys into falls by design. The gain per trade is small, about the distance
back to the average; the loss has no limit.

## Parameters
`period` 20, `width` 2.0 standard deviations.

## Code
`bollinger_reversion` in `market/strategies.py`.
