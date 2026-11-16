---
key: covered_call
name: Covered call
kind: option
regime: ranging
view: neutral to mildly bullish
code: pending
---
# Covered call

## Summary
Own the shares and sell a call above the current price. The premium is income
now. In return, the gain above the strike is given up: if the stock rallies
past it, the shares are called away at the strike.

## Rules
Entry: hold the shares, matching one lot, and sell one out-of-the-money call.
Exit: buy the call back, or let it expire worthless and sell another, or
deliver the shares at the strike if assigned.

## Regime
Flat or slowly rising markets, where the stock is unlikely to pass the strike
before expiry.

## When it fails
A sharp rally: the upside is capped at the strike plus the premium. A sharp
fall: the premium cushions it only a little, and the shares take the rest.

## Risk
Nearly the full downside of owning the stock, less the premium. Maximum loss:
purchase price - premium, if the stock goes to zero. Breakeven at expiry:
purchase price - premium. Maximum gain: strike - purchase price + premium.

## Parameters
Call strike, days to expiry.

## Code
Pending: step 7C phase 5.
