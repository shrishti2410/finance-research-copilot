---
key: long_put
name: Long put
kind: option
regime: downtrend
view: bearish
code: market/options.py#long_put
---
# Long put

## Summary
Buy a put option: the right to sell the stock at the strike price. It gains
as the stock falls below the strike, and the loss is capped at the premium.
In India it is the simplest way for an individual to profit from a fall,
since a delivery short cannot be held overnight.

## Rules
Entry: buy one put at or slightly below the current price. Exit: sell before
expiry, or let it expire, when it is worth max(strike - stock, 0).

## Regime
A downtrend, or an expected sharp fall.

## When it fails
The fall does not come before expiry, or is smaller than the premium. Time
decay works against the buyer every day.

## Risk
Maximum loss: the premium. Breakeven at expiry: strike - premium. Maximum
gain: strike - premium, if the stock goes to zero.

## Parameters
Strike, days to expiry, lots.

## Code
`long_put` in `market/options.py`: legs, Black-Scholes premium estimates from historical volatility, payoff at expiry, breakevens and maximum profit and loss, with every assumption listed in the result.
