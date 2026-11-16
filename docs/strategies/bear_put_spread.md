---
key: bear_put_spread
name: Bear put spread
kind: option
regime: downtrend
view: moderately bearish
code: pending
---
# Bear put spread

## Summary
Buy a put at one strike and sell a put at a lower strike, same expiry: the
mirror image of the bull call spread. Cheaper than a plain long put, with the
gain capped at the lower strike.

## Rules
Entry: buy a put near the current price and sell one further down. Exit:
close both legs together, or hold to expiry.

## Regime
A downtrend with a rough target.

## When it fails
The stock stays above the higher strike and both puts expire worthless. A
crash earns no more than the cap.

## Risk
Maximum loss: the net premium paid. Maximum gain: the difference between the
strikes, less the net premium. Breakeven at expiry: higher strike - net
premium.

## Parameters
Both strikes, days to expiry.

## Code
Pending: step 7C phase 5.
