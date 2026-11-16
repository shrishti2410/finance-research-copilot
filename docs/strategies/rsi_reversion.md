---
key: rsi_reversion
name: RSI mean reversion
kind: equity
regime: ranging
code: market/strategies.py#rsi_reversion
---
# RSI mean reversion

## Summary
RSI compares the size of recent up days with recent down days on a 0 to 100
scale. Below 30 is conventionally "oversold". The strategy buys an oversold
stock and holds until it is "overbought" (above 70), betting that a sharp
fall is followed by a bounce.

## Rules
Entry: 14-day RSI below 30. Exit: RSI above 70.

## Regime
Range-bound markets where the price oscillates around a stable level.

## When it fails
In a real downtrend RSI can stay below 30 while the price keeps falling, and
"oversold" becomes "cheaper and cheaper". The exit at 70 can take a long time
to come, so it may hold through a further fall.

## Risk
No stop-loss, so the worst trades are large. It buys exactly when the news is
bad, which is the point and also the danger.

## Parameters
`period` 14 (Wilder's original), `buy_below` 30, `sell_above` 70.

## Code
`rsi_reversion` in `market/strategies.py`.
