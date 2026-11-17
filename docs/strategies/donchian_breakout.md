---
key: donchian_breakout
name: Donchian channel breakout (Turtle)
kind: equity
regime: trending
code: market/strategies.py#donchian_breakout
---
# Donchian channel breakout (Turtle)

## Summary
Buy when the price closes above its highest high of the previous 20 days, and
sell when it closes below its lowest low of the previous 10. This is the core
of the "Turtle" rules from the 1980s: no forecasting, just following new
highs.

## Rules
Entry: close above the prior 20-day high (today excluded). Exit: close below
the prior 10-day low.

## Regime
Strong directional trends.

## When it fails
In a range every new 20-day high is near the top of the range. Breakout
systems like this often win fewer than half their trades and rely on a few
large trends to pay for the rest.

## Risk
The exit sits at the 10-day low, which in a volatile stock can be 10% or more
below the entry.

## Parameters
`entry` 20, `exit` 10 days.

## Code
`donchian_breakout` in `market/strategies.py`.
