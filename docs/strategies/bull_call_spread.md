---
key: bull_call_spread
name: Bull call spread
kind: option
regime: trending
view: moderately bullish
code: market/options.py#bull_call_spread
---
# Bull call spread

## Summary
Buy a call at one strike and sell a call at a higher strike, same expiry. The
sold call pays for part of the bought one, so the trade is cheaper than a
long call. In exchange, the gain is capped at the higher strike.

## Rules
Entry: buy a call near the current price and sell one further up. Exit: close
both legs together before expiry, or hold to expiry.

## Regime
A steady uptrend with a rough target in mind.

## When it fails
The stock stays below the lower strike, so both calls expire worthless and
the net premium is lost. A large rally earns no more than the cap.

## Risk
Maximum loss: the net premium paid. Maximum gain: the difference between the
strikes, less the net premium. Breakeven at expiry: lower strike + net
premium.

## Parameters
Both strikes, days to expiry.

## Code
`bull_call_spread` in `market/options.py`: legs, Black-Scholes premium estimates from historical volatility, payoff at expiry, breakevens and maximum profit and loss, with every assumption listed in the result.
