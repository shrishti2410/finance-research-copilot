"""Option Greeks: textbook values, finite differences, and a structure's sum.

The textbook case is S = K = 100, one year, r = 5%, sigma = 20%: d1 = 0.35,
d2 = 0.15, N(d1) = 0.6368, n(d1) = 0.3752, N(d2) = 0.5596. From those, by the
standard formulas: call delta 0.6368, put delta -0.3632, gamma 0.01876, vega
37.52 per unit of volatility, call theta -6.414 and put theta -1.657 per year.
"""

from __future__ import annotations

import pytest

from market import options as op

S, K, T, R, V = 100.0, 100.0, 1.0, 0.05, 0.2


def test_greeks_match_the_textbook_case():
    call = op.greeks(S, K, T, R, V, "call")
    put = op.greeks(S, K, T, R, V, "put")
    assert call["delta"] == pytest.approx(0.6368, abs=1e-4)
    assert put["delta"] == pytest.approx(-0.3632, abs=1e-4)
    assert call["gamma"] == pytest.approx(0.01876, abs=1e-5) == put["gamma"]
    assert call["vega"] == pytest.approx(37.52, abs=0.01) and put["vega"] == pytest.approx(37.52, abs=0.01)
    assert call["theta"] == pytest.approx(-6.414, abs=0.002)
    assert put["theta"] == pytest.approx(-1.657, abs=0.002)


@pytest.mark.parametrize("kind", ["call", "put"])
@pytest.mark.parametrize("spot, strike, years, sigma", [(100, 100, 1, 0.2), (1090.5, 1140, 30 / 365, 0.22),
                                                        (50, 40, 0.25, 0.6)])
def test_greeks_agree_with_finite_differences_of_the_price(kind, spot, strike, years, sigma):
    g = op.greeks(spot, strike, years, R, sigma, kind)
    price = lambda s=spot, t=years, v=sigma: op.bs_price(s, strike, t, R, v, kind)  # noqa: E731
    h = spot * 1e-4
    assert g["delta"] == pytest.approx((price(spot + h) - price(spot - h)) / (2 * h), rel=1e-4, abs=1e-6)
    assert g["gamma"] == pytest.approx((price(spot + h) - 2 * price() + price(spot - h)) / h ** 2, rel=1e-3, abs=1e-6)
    dv = 1e-5
    assert g["vega"] == pytest.approx((price(v=sigma + dv) - price(v=sigma - dv)) / (2 * dv), rel=1e-4)
    dt = 1e-6
    # Theta is the change in value as time passes: minus the derivative in T.
    assert g["theta"] == pytest.approx(-(price(t=years + dt) - price(t=years - dt)) / (2 * dt), rel=1e-3, abs=1e-4)


def test_put_call_parity_holds_for_the_greeks():
    import math
    call, put = op.greeks(S, K, T, R, V, "call"), op.greeks(S, K, T, R, V, "put")
    assert call["delta"] - put["delta"] == pytest.approx(1.0)
    assert call["theta"] - put["theta"] == pytest.approx(-R * K * math.exp(-R * T))


def test_a_structure_reports_the_sum_of_its_legs_per_day_and_per_vol_point():
    q = op.option_structure("bull_call_spread", 1000.0, 0.25)
    years = q["days"] / 365
    legs = [op.greeks(1000.0, leg["strike"], years, op.RISK_FREE_RATE, 0.25, leg["kind"])
            for leg in q["legs"]]
    sides = [1 if leg["side"] == "buy" else -1 for leg in q["legs"]]
    g = q["greeks"]
    assert g["delta"] == pytest.approx(sum(s * x["delta"] for s, x in zip(sides, legs)), abs=1e-4)
    assert g["gamma"] == pytest.approx(sum(s * x["gamma"] for s, x in zip(sides, legs)), abs=1e-6)
    assert g["theta_per_day"] == pytest.approx(sum(s * x["theta"] for s, x in zip(sides, legs)) / 365, abs=1e-4)
    assert g["vega_per_vol_point"] == pytest.approx(sum(s * x["vega"] for s, x in zip(sides, legs)) / 100, abs=1e-4)
    assert 0 < g["delta"] < 1                       # a bull spread is long the stock, less than one share


def test_a_stock_leg_counts_as_delta_one_and_lot_size_scales_everything():
    cc = op.option_structure("covered_call", 1000.0, 0.25)
    call = op.greeks(1000.0, cc["legs"][1]["strike"], cc["days"] / 365, op.RISK_FREE_RATE, 0.25, "call")
    assert cc["greeks"]["delta"] == pytest.approx(1 - call["delta"], abs=1e-4)
    lot = op.option_structure("covered_call", 1000.0, 0.25, lot_size=100)
    for key in ("delta", "gamma", "theta_per_day", "vega_per_vol_point"):
        assert lot["greeks"][key] == pytest.approx(cc["greeks"][key] * 100, rel=1e-3, abs=1e-4)


def test_an_expired_option_has_no_time_value_greeks():
    g = op.greeks(110, 100, 0, R, V, "call")
    assert g["delta"] == 1.0 and g["gamma"] == 0 and g["vega"] == 0 and g["theta"] == 0
