---
key: protective_put
name: Protective put
kind: option
regime: any
view: bullish, with insurance
code: pending
---
# Protective put

## Summary
Own the shares and buy a put below the current price. The put is insurance:
below its strike, the loss on the shares is offset. The premium is the cost
of the insurance, paid whether or not it is needed.

## Rules
Entry: hold the shares and buy one put per lot, a little below the current
price. Exit: sell the put when the risk has passed, or let it expire.

## Regime
Any. It is used around events such as results or elections, or after a large
gain an investor wants to protect.

## When it fails
It is not so much a failure as a cost: if the stock never falls, the premium
is lost. Bought repeatedly, puts are an expensive drag on returns.

## Risk
Maximum loss: purchase price - strike + premium. Breakeven at expiry:
purchase price + premium. The upside is unlimited, less the premium.

## Parameters
Put strike (how much loss is accepted before the insurance pays), days to
expiry.

## Code
Pending: step 7C phase 5.
