---
key: long_call
name: Long call
kind: option
regime: trending
view: bullish
code: pending
---
# Long call

## Summary
Buy a call option: the right, not the obligation, to buy the stock at the
strike price on or before expiry. It is a bet that the stock rises above the
strike by more than the premium paid, with the loss capped at that premium.

## Rules
Entry: buy one call, usually at or slightly above the current price, with
enough time to expiry for the expected move. Exit: sell the option before
expiry, or let it expire. At expiry it is worth max(stock - strike, 0).

## Regime
A clear uptrend, or an expected sharp move up. Best when implied volatility
is low, because a low premium is easier to recover.

## When it fails
The stock rises too little or too late: time decay eats the premium every
day, so being right slowly can still lose money. A fall in volatility also
lowers the option's value even if the stock does not move.

## Risk
Maximum loss: the premium paid. Breakeven at expiry: strike + premium.
Maximum gain: unlimited in principle. NSE stock options trade in exchange-set
lots, so the smallest position can be large in rupees.

## Parameters
Strike (moneyness), days to expiry, number of lots.

## Code
Pending: step 7C phase 5 adds the payoff, breakeven and a Black-Scholes
premium estimate.
