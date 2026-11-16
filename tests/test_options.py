"""Black-Scholes, historical volatility, and the payoff of each structure.

The pricing is checked against the textbook case (S = K = 100, one year,
r = 5%, sigma = 20%: call 10.4506, put 5.5735) and put-call parity. Each
structure's max profit, max loss and breakevens are checked against the
hand formula on its own card in docs/strategies/.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from market import options as op


def test_black_scholes_matches_the_textbook_case():
    assert op.bs_price(100, 100, 1.0, 0.05, 0.2, "call") == pytest.approx(10.4506, abs=1e-4)
    assert op.bs_price(100, 100, 1.0, 0.05, 0.2, "put") == pytest.approx(5.5735, abs=1e-4)


@pytest.mark.parametrize("spot, strike, years, rate, sigma", [
    (1090.5, 1100, 30 / 365, 0.065, 0.23), (50, 70, 0.5, 0.0, 0.6), (300, 250, 2, 0.1, 0.15)])
def test_put_call_parity(spot, strike, years, rate, sigma):
    call = op.bs_price(spot, strike, years, rate, sigma, "call")
    put = op.bs_price(spot, strike, years, rate, sigma, "put")
    assert call - put == pytest.approx(spot - strike * math.exp(-rate * years), abs=1e-9)


def test_an_expired_or_volatility_free_option_is_worth_its_intrinsic_value():
    assert op.bs_price(110, 100, 0, 0.05, 0.2, "call") == 10
    assert op.bs_price(110, 100, 0.5, 0.05, 0, "put") == 0
    with pytest.raises(ValueError):
        op.bs_price(100, 100, 1, 0.05, 0.2, "straddle")


def test_historical_volatility_is_the_annualised_sd_of_log_returns():
    rng = np.random.default_rng(1)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.015, 60)))
    r = np.diff(np.log(closes[-31:]))
    assert op.historical_volatility(closes) == pytest.approx(np.std(r, ddof=1) * math.sqrt(252))
    with pytest.raises(ValueError, match="31 closes"):
        op.historical_volatility(closes[:20])


def _q(key, spot=1000.0, sigma=0.25, **kw):
    return op.option_structure(key, spot, sigma, **kw)


def _prem(q, i):
    return q["legs"][i]["premium_estimate"]


def test_long_call_loses_at_most_its_premium_and_breaks_even_above_the_strike():
    q = _q("long_call")
    k, p = q["legs"][0]["strike"], _prem(q, 0)
    assert q["max_loss"] == pytest.approx(-p, abs=0.01)
    assert q["max_profit_unbounded"] and q["max_profit"] is None
    assert q["breakevens"] == [pytest.approx(k + p, abs=0.01)]
    assert q["net_premium_estimate"] == pytest.approx(-p, abs=0.01)


def test_long_put_gains_at_most_strike_minus_premium():
    q = _q("long_put")
    k, p = q["legs"][0]["strike"], _prem(q, 0)
    assert q["max_loss"] == pytest.approx(-p, abs=0.01)
    assert q["max_profit"] == pytest.approx(k - p, abs=0.01)
    assert q["breakevens"] == [pytest.approx(k - p, abs=0.01)]


def test_spreads_are_capped_both_ways():
    bull = _q("bull_call_spread")
    (k1, k2), net = (bull["legs"][0]["strike"], bull["legs"][1]["strike"]), _prem(bull, 0) - _prem(bull, 1)
    assert bull["max_loss"] == pytest.approx(-net, abs=0.02)
    assert bull["max_profit"] == pytest.approx(k2 - k1 - net, abs=0.02)
    assert bull["breakevens"] == [pytest.approx(k1 + net, abs=0.02)]
    bear = _q("bear_put_spread")
    (k1, k2), net = (bear["legs"][0]["strike"], bear["legs"][1]["strike"]), _prem(bear, 0) - _prem(bear, 1)
    assert bear["max_loss"] == pytest.approx(-net, abs=0.02)
    assert bear["max_profit"] == pytest.approx(k1 - k2 - net, abs=0.02)
    assert bear["breakevens"] == [pytest.approx(k1 - net, abs=0.02)]


def test_a_straddle_has_two_breakevens_and_loses_both_premiums_at_the_strike():
    q = _q("long_straddle")
    k, total = q["legs"][0]["strike"], _prem(q, 0) + _prem(q, 1)
    assert q["max_loss"] == pytest.approx(-total, abs=0.02)
    assert q["breakevens"] == [pytest.approx(k - total, abs=0.02), pytest.approx(k + total, abs=0.02)]
    assert q["max_profit_unbounded"]


def test_an_iron_condor_keeps_its_credit_or_loses_one_wing_less_the_credit():
    q = _q("iron_condor")
    strikes = [leg["strike"] for leg in q["legs"]]
    credit = -_prem(q, 0) + _prem(q, 1) + _prem(q, 2) - _prem(q, 3)
    assert credit > 0 and q["net_premium_estimate"] == pytest.approx(credit, abs=0.02)
    assert q["max_profit"] == pytest.approx(credit, abs=0.02)
    wing = max(strikes[1] - strikes[0], strikes[3] - strikes[2])
    assert q["max_loss"] == pytest.approx(-(wing - credit), abs=0.02)
    assert q["breakevens"] == [pytest.approx(strikes[1] - credit, abs=0.02),
                               pytest.approx(strikes[2] + credit, abs=0.02)]


def test_covered_call_and_protective_put_match_their_cards():
    cc = _q("covered_call", spot=1090.5)
    k, p = cc["legs"][1]["strike"], _prem(cc, 1)
    assert cc["max_profit"] == pytest.approx(k - 1090.5 + p, abs=0.02)
    assert cc["max_loss"] == pytest.approx(-(1090.5 - p), abs=0.02)      # stock to zero
    assert cc["breakevens"] == [pytest.approx(1090.5 - p, abs=0.02)]
    pp = _q("protective_put", spot=1090.5)
    k, p = pp["legs"][1]["strike"], _prem(pp, 1)
    assert pp["max_loss"] == pytest.approx(-(1090.5 - k + p), abs=0.02)
    assert pp["max_profit_unbounded"]
    assert pp["breakevens"] == [pytest.approx(1090.5 + p, abs=0.02)]


def test_lot_size_scales_money_not_prices_and_the_payoff_curve_agrees():
    one, lot = _q("bull_call_spread"), _q("bull_call_spread", lot_size=750)
    assert lot["max_loss"] == pytest.approx(one["max_loss"] * 750, abs=10)
    assert lot["breakevens"] == one["breakevens"]
    assert len(one["payoff"]) == 61
    assert min(p["profit"] for p in one["payoff"]) >= one["max_loss"] - 0.01
    assert max(p["profit"] for p in one["payoff"]) <= one["max_profit"] + 0.01


def test_strikes_are_round_numbers_and_every_result_carries_its_assumptions():
    q = _q("iron_condor", spot=1090.5)
    assert q["strike_step"] == 20
    assert all(leg["strike"] % 20 == 0 for leg in q["legs"])
    assert any("not market quotes" in a for a in q["assumptions"])
    assert q["disclaimer"] == "Educational project, not investment advice."
    with pytest.raises(KeyError, match="Known"):
        _q("butterfly")


def test_structures_follow_the_engine_output():
    assert op.structures_for_signal("BUY")[0] == "bull_call_spread"
    assert op.structures_for_signal("SELL")[0] == "bear_put_spread"
    assert "iron_condor" in op.structures_for_signal("HOLD")
    assert "long_straddle" in op.structures_for_signal("BUY", "volatile")


def test_every_options_card_has_a_structure_and_links_to_it():
    from rag.strategy_cards import load_cards
    cards = {c.key: c for c in load_cards() if c.kind == "option"}
    assert set(cards) == set(op.STRUCTURES)
    for key, card in cards.items():
        assert card.code == f"market/options.py#{key}", card.code


def test_option_ideas_follow_the_signal_and_carry_its_verdict():
    import asyncio

    from market import service
    from tests.test_markets_costs import _Recorder

    out = asyncio.run(service.option_ideas("SBIN.NS", data=_Recorder()))
    assert out["currency"] == "INR" and out["volatility_30d"] > 0
    assert out["signal"]["signal"] in ("BUY", "SELL", "HOLD") and " of " in out["signal"]["coverage"]
    assert out["backtest"]["verdict"] in ("edge", "no_edge", "untested")
    expected = op.structures_for_signal(out["signal"]["signal"], out["signal"]["regime"])
    assert [s["structure"] for s in out["structures"]] == expected
    one = asyncio.run(service.option_ideas("SBIN.NS", "iron_condor", days=45, lot_size=10,
                                           data=_Recorder()))
    assert [s["structure"] for s in one["structures"]] == ["iron_condor"]
    assert one["structures"][0]["days"] == 45 and one["structures"][0]["lot_size"] == 10
    assert out["disclaimer"] == "Educational project, not investment advice."
