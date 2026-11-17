---
key: rsi2_pullback
name: RSI(2) pullback in an uptrend
kind: equity
regime: trending
code: market/strategies.py#rsi2_pullback
---
# RSI(2) pullback in an uptrend

## Summary
Buy a short, sharp dip in a stock that is in a long-term uptrend. A 2-day RSI
reacts to a single bad day or two. The 200-day filter keeps the rule from
buying dips in a stock that is falling for good. The idea was popularised by
Larry Connors.

## Rules
Entry: 2-day RSI below 10 and the close above its 200-day SMA. Exit: a close
above the 5-day SMA, usually within a few days.

## Regime
Uptrends with frequent short pullbacks. It combines a trend filter with a
mean-reversion trigger.

## When it fails
When the uptrend ends: the first dips of a new decline still pass the 200-day
filter. Many short trades, so costs weigh more than on slow rules.

## Risk
No stop-loss inside the trade. Small average gains, so a few large losses can
erase many wins.

## Parameters
`period` 2, `buy_below` 10, `trend` 200, `exit_sma` 5.

## Code
`rsi2_pullback` in `market/strategies.py`.
