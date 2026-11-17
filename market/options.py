"""Option structures for study: estimated premiums, payoff at expiry, breakeven, max loss.

    quote = option_structure("bull_call_spread", spot=1090.5, sigma=0.23, days=30)
    menu  = structures_for_signal("BUY", "ranging")

**Every premium here is an estimate, not a market price.** It is the
Black-Scholes value from the stock's own historical volatility. Real NSE
option premiums carry the market's implied volatility, which is usually
different, and bid-ask spreads this does not see. Read the figures as "what
the textbook model says this structure would cost if volatility stayed as it
has been", nothing more.

Assumptions, each a parameter and each returned with the result:

- **Volatility:** annualised standard deviation of the last 30 daily log
  returns of the split-adjusted close (`historical_volatility`).
- **Risk-free rate:** `RISK_FREE_RATE` = 6.5% a year, continuously
  compounded. That is an assumed round figure in the range of Indian
  short-term government rates; it was not looked up on any date. The premium
  of a 30-day option moves little with it (a percentage point changes an
  at-the-money premium by about 0.04% of the stock price).
- **Dividends:** none.
- **Exercise:** European, at expiry only. NSE stock options are European.
- **Strikes:** hypothetical, rounded to `strike_step(spot)`. These are round
  numbers near the stock price, not NSE's listed strikes.
- **Expiry:** `days` calendar days from today, 30 by default.
- **Size:** per share. NSE trades options in exchange-set lots; pass
  `lot_size` to scale, and check the exchange's current lot size. It is not
  guessed here.
- **Costs:** brokerage, STT and exchange charges on options are not included.

Payoffs are piecewise linear in the price at expiry, so maximum profit and
loss are found exactly: at a kink (a strike), at zero, or unbounded when the
payoff keeps rising or falling beyond the last strike.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

__all__ = ["RISK_FREE_RATE", "DEFAULT_DAYS", "VOL_WINDOW", "STRUCTURES", "Leg", "bs_price", "greeks",
           "historical_volatility", "strike_step", "option_structure",
           "structures_for_signal", "ASSUMPTIONS", "DISCLAIMER"]

RISK_FREE_RATE = 0.065
DEFAULT_DAYS = 30
VOL_WINDOW = 30
DISCLAIMER = "Educational project, not investment advice."
ASSUMPTIONS = (
    "Premiums are Black-Scholes estimates from historical volatility, not market quotes.",
    "Risk-free rate 6.5% a year: an assumed round figure, not a looked-up rate.",
    "No dividends; European exercise; strikes are hypothetical round numbers.",
    "Per share: multiply by the exchange's lot size for one contract.",
    "Brokerage, STT and exchange charges on options are not included.",
    "Greeks are Black-Scholes values at today's price and volatility: theta per calendar day, vega per volatility point.",
)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, years: float, rate: float, sigma: float,
             kind: str) -> float:
    """Black-Scholes value of a European call or put, no dividends."""
    if kind not in ("call", "put"):
        raise ValueError(f"kind must be 'call' or 'put', not {kind!r}")
    if years <= 0 or sigma <= 0:
        intrinsic = spot - strike if kind == "call" else strike - spot
        return max(intrinsic, 0.0)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma ** 2) * years) / (sigma * math.sqrt(years))
    d2 = d1 - sigma * math.sqrt(years)
    disc = math.exp(-rate * years)
    if kind == "call":
        return spot * _cdf(d1) - strike * disc * _cdf(d2)
    return strike * disc * _cdf(-d2) - spot * _cdf(-d1)


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def greeks(spot: float, strike: float, years: float, rate: float, sigma: float,
           kind: str) -> dict[str, float]:
    """Black-Scholes Greeks of one European option, no dividends.

    delta  change in value per 1 change in the stock price
    gamma  change in delta per 1 change in the stock price
    theta  change in value per year that passes (negative: time decay)
    vega   change in value per 1.00 (100 points) of volatility

    An expired option, or one with no volatility, has delta 1, 0 or -1 by
    moneyness and no gamma, theta or vega.
    """
    if kind not in ("call", "put"):
        raise ValueError(f"kind must be 'call' or 'put', not {kind!r}")
    if years <= 0 or sigma <= 0:
        itm = spot > strike if kind == "call" else spot < strike
        return {"delta": (1.0 if kind == "call" else -1.0) if itm else 0.0,
                "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    root = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma ** 2) * years) / (sigma * root)
    d2 = d1 - sigma * root
    disc = math.exp(-rate * years)
    decay = -spot * _pdf(d1) * sigma / (2.0 * root)
    if kind == "call":
        delta = _cdf(d1)
        theta = decay - rate * strike * disc * _cdf(d2)
    else:
        delta = _cdf(d1) - 1.0
        theta = decay + rate * strike * disc * _cdf(-d2)
    return {"delta": delta, "gamma": _pdf(d1) / (spot * sigma * root),
            "theta": theta, "vega": spot * _pdf(d1) * root}


def historical_volatility(closes, window: int = VOL_WINDOW) -> float:
    """Annualised standard deviation of the last `window` daily log returns."""
    c = np.asarray(closes, dtype=float)
    if len(c) < window + 1:
        raise ValueError(f"need {window + 1} closes for a {window}-day volatility, have {len(c)}")
    r = np.diff(np.log(c[-(window + 1):]))
    return float(np.std(r, ddof=1) * math.sqrt(252))


def strike_step(spot: float) -> float:
    """A round strike interval for a stock price. An approximation for
    choosing hypothetical strikes, not NSE's published strike scheme."""
    for limit, step in ((250, 2.5), (500, 5), (1000, 10), (2500, 20), (5000, 50)):
        if spot < limit:
            return step
    return 100.0


@dataclass(frozen=True)
class Leg:
    kind: str            # "call", "put" or "stock"
    side: int            # +1 bought, -1 sold
    strike: float | None = None
    premium: float = 0.0  # per share; for stock, the purchase price

    def payoff(self, price: np.ndarray) -> np.ndarray:
        """Profit per share at expiry, premium included."""
        if self.kind == "stock":
            return self.side * (price - self.premium)
        intrinsic = np.maximum(price - self.strike, 0) if self.kind == "call" \
            else np.maximum(self.strike - price, 0)
        return self.side * (intrinsic - self.premium)


@dataclass(frozen=True)
class Structure:
    key: str
    name: str
    view: str
    build: callable = field(repr=False)     # (spot, step) -> [(kind, side, strike|None)]


def _r(x: float, step: float) -> float:
    return round(x / step) * step


STRUCTURES: dict[str, Structure] = {s.key: s for s in [
    Structure("long_call", "Long call", "bullish",
              lambda s, k: [("call", +1, _r(s, k))]),
    Structure("long_put", "Long put", "bearish",
              lambda s, k: [("put", +1, _r(s, k))]),
    Structure("covered_call", "Covered call", "neutral to mildly bullish",
              lambda s, k: [("stock", +1, None), ("call", -1, _r(s * 1.05, k))]),
    Structure("protective_put", "Protective put", "bullish, with insurance",
              lambda s, k: [("stock", +1, None), ("put", +1, _r(s * 0.95, k))]),
    Structure("bull_call_spread", "Bull call spread", "moderately bullish",
              lambda s, k: [("call", +1, _r(s, k)), ("call", -1, _r(s * 1.05, k))]),
    Structure("bear_put_spread", "Bear put spread", "moderately bearish",
              lambda s, k: [("put", +1, _r(s, k)), ("put", -1, _r(s * 0.95, k))]),
    Structure("long_straddle", "Long straddle", "a big move, direction unknown",
              lambda s, k: [("call", +1, _r(s, k)), ("put", +1, _r(s, k))]),
    Structure("iron_condor", "Iron condor", "the stock stays in a range",
              lambda s, k: [("put", +1, _r(s * 0.90, k)), ("put", -1, _r(s * 0.95, k)),
                            ("call", -1, _r(s * 1.05, k)), ("call", +1, _r(s * 1.10, k))]),
]}


def _total(legs: list[Leg], price) -> np.ndarray:
    price = np.asarray(price, dtype=float)
    return sum((leg.payoff(price) for leg in legs), np.zeros_like(price))


def _extremes(legs: list[Leg]) -> dict:
    """Exact max profit, max loss and breakevens of a piecewise-linear payoff."""
    kinks = sorted({leg.strike for leg in legs if leg.strike is not None})
    points = np.array([0.0, *kinks])
    values = _total(legs, points)
    top = (kinks[-1] if kinks else 0.0) * 2 + 1.0
    slope_up = float(_total(legs, [top + 1.0])[0] - _total(legs, [top])[0])
    candidates = list(values)
    max_profit = None if slope_up > 1e-9 else float(max(candidates + [_total(legs, [top])[0]]))
    max_loss = None if slope_up < -1e-9 else float(min(candidates + [_total(legs, [top])[0]]))
    # Breakevens: sign changes between consecutive kinks, and beyond the last.
    grid = np.array([0.0, *kinks, top])
    vals = _total(legs, grid)
    breakevens = []
    for a, b, va, vb in zip(grid, grid[1:], vals, vals[1:]):
        if va == 0:
            breakevens.append(float(a))
        elif va * vb < 0:
            breakevens.append(float(a + (b - a) * (-va) / (vb - va)))
    if vals[-1] == 0:
        breakevens.append(float(grid[-1]))
    elif slope_up != 0 and vals[-1] * slope_up < 0:
        breakevens.append(float(top - vals[-1] / slope_up))
    return {"max_profit": max_profit, "max_loss": max_loss,
            "breakevens": sorted({round(b, 2) for b in breakevens})}


def option_structure(key: str, spot: float, sigma: float, days: int = DEFAULT_DAYS,
                     rate: float = RISK_FREE_RATE, lot_size: int = 1,
                     points: int = 61) -> dict:
    """One structure, priced and analysed. Money figures are per share times
    `lot_size`; `max_profit` or `max_loss` is None when unbounded."""
    if key not in STRUCTURES:
        raise KeyError(f"Unknown structure {key!r}. Known: {', '.join(STRUCTURES)}.")
    if spot <= 0 or sigma <= 0 or days <= 0:
        raise ValueError("spot, sigma and days must be positive")
    st = STRUCTURES[key]
    step = strike_step(spot)
    years = days / 365.0
    legs = []
    for kind, side, strike in st.build(spot, step):
        premium = spot if kind == "stock" else bs_price(spot, strike, years, rate, sigma, kind)
        legs.append(Leg(kind, side, strike, round(premium, 4)))
    ext = _extremes(legs)
    # The structure's Greeks: the sum over its legs, signed by side; a stock
    # leg has delta 1 and nothing else. Theta per calendar day, vega per one
    # point of volatility, so both read in rupees (or dollars) per share.
    total = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in legs:
        if leg.kind == "stock":
            total["delta"] += leg.side
            continue
        g = greeks(spot, leg.strike, years, rate, sigma, leg.kind)
        for greek in total:
            total[greek] += leg.side * g[greek]
    net = sum(-leg.side * leg.premium for leg in legs if leg.kind != "stock")
    lo, hi = spot * 0.7, spot * 1.3
    # The strikes and breakevens are in the grid, so a chart drawn from it has
    # its kinks and zero crossings exactly where they are, not between samples.
    exact = [leg.strike for leg in legs if leg.strike is not None] + ext["breakevens"]
    grid = np.unique(np.concatenate([np.linspace(lo, hi, points),
                                     [x for x in exact if lo <= x <= hi]]))
    pay = _total(legs, grid)

    def scale(x):
        return None if x is None else round(x * lot_size, 2)

    return {
        "structure": key, "name": st.name, "view": st.view,
        "spot": round(spot, 2), "volatility": round(sigma, 4), "days": days,
        "rate": rate, "lot_size": lot_size, "strike_step": step,
        "legs": [{"kind": leg.kind, "side": "buy" if leg.side > 0 else "sell",
                  "strike": leg.strike, "premium_estimate": round(leg.premium, 2)}
                 for leg in legs],
        # Positive: money received for the options; negative: paid.
        "net_premium_estimate": scale(net),
        "max_profit": scale(ext["max_profit"]), "max_loss": scale(ext["max_loss"]),
        "max_profit_unbounded": ext["max_profit"] is None,
        "max_loss_unbounded": ext["max_loss"] is None,
        "breakevens": ext["breakevens"],
        "greeks": {"delta": round(total["delta"] * lot_size, 4),
                   "gamma": round(total["gamma"] * lot_size, 6),
                   "theta_per_day": round(total["theta"] / 365 * lot_size, 4),
                   "vega_per_vol_point": round(total["vega"] / 100 * lot_size, 4)},
        "payoff": [{"price": round(float(p), 2), "profit": round(float(v) * lot_size, 2)}
                   for p, v in zip(grid, pay)],
        "card": f"docs/strategies/{key}.md",
        "assumptions": list(ASSUMPTIONS),
        "disclaimer": DISCLAIMER,
    }


# Which structures express the engine's output, for study. Not a
# recommendation: each is listed with what has to happen for it to pay.
_FOR_SIGNAL = {
    "BUY": ("bull_call_spread", "long_call", "covered_call"),
    "SELL": ("bear_put_spread", "long_put", "protective_put"),
    "HOLD": ("iron_condor", "covered_call", "long_straddle"),
}


def structures_for_signal(signal: str, regime: str | None = None) -> list[str]:
    """The structures whose market view matches the engine's output. In a
    volatile regime the straddle (a bet on a large move either way) is added."""
    keys = list(_FOR_SIGNAL.get(signal, _FOR_SIGNAL["HOLD"]))
    if regime == "volatile" and "long_straddle" not in keys:
        keys.append("long_straddle")
    return keys
