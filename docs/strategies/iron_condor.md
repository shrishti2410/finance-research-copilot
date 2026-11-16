---
key: iron_condor
name: Iron condor
kind: option
regime: ranging
view: the stock stays in a range
code: pending
---
# Iron condor

## Summary
Sell an out-of-the-money put and an out-of-the-money call. Then buy a further
put below and a further call above, as protection. The net premium received
is the profit if the stock stays between the two sold strikes until expiry.

## Rules
Entry: four legs on the same expiry. From low to high: buy put, sell put,
sell call, buy call. Exit: buy the position back early once most of the
premium is earned, or hold to expiry.

## Regime
Quiet, range-bound markets with falling or stable volatility.

## When it fails
A breakout in either direction. Many small wins can be undone by one large
loss, and the loss arrives fast near expiry.

## Risk
Maximum gain: the net premium received. Maximum loss: the width of one wing
(the gap between a sold and a bought strike), less the net premium.
Breakevens at expiry: sold put strike - net premium, and sold call strike +
net premium. Selling options needs margin, which the exchange sets and can
raise.

## Parameters
Four strikes (the distance of the sold strikes and the width of the wings),
days to expiry.

## Code
Pending: step 7C phase 5.
