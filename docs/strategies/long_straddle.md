---
key: long_straddle
name: Long straddle
kind: option
regime: volatile
view: a big move, direction unknown
code: pending
---
# Long straddle

## Summary
Buy a call and a put at the same strike, near the current price, same expiry.
It profits from a large move either way and loses if the stock stays put. It
is a bet on volatility, not on direction.

## Rules
Entry: buy one at-the-money call and one at-the-money put. Exit: sell both
after the move, or at expiry.

## Regime
Just before an event that could move the stock sharply: results, a court
ruling, a policy decision. Best when implied volatility is still low.

## When it fails
The move is smaller than the two premiums combined, which is common: option
prices before an event usually already include a large expected move. After
the event implied volatility drops (a "volatility crush"), cutting both legs'
value at once.

## Risk
Maximum loss: both premiums, if the stock ends exactly at the strike.
Breakevens at expiry: strike plus or minus the total premium. The gain is
unlimited upward and large downward.

## Parameters
Strike, days to expiry.

## Code
Pending: step 7C phase 5.
