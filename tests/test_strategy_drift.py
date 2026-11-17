"""Strategy drift detection (backlog item 5): a lower CUSUM on a rule's recent
returns against its own history, the flag in the evidence, and the optional
down-weighting. Synthetic data only."""

from __future__ import annotations

import numpy as np
import pytest

from market import drift as dr
from market import signals as sg
from market.markets import NSE
from tests.test_signals import BARS


def test_the_lower_cusum_accumulates_shortfalls_beyond_the_allowance():
    # z: 0, -1, -2, +3 with k = 0.5: S = 0, 0.5, 2.0, 0 -> max 2.0
    assert dr.lower_cusum(np.array([0.0, -1.0, -2.0, 3.0]), k=0.5) == pytest.approx(2.0)
    assert dr.lower_cusum(np.array([1.0, 2.0, 0.4]), k=0.5) == 0.0


def _returns(n_hist, n_recent, recent_shift, seed):
    rng = np.random.default_rng(seed)
    hist = rng.normal(0.0008, 0.01, n_hist)
    recent = rng.normal(0.0008 + recent_shift, 0.01, n_recent)
    return np.concatenate([hist, recent])


def test_a_clear_drop_in_recent_returns_is_flagged():
    r = _returns(441, 63, -0.008, seed=1)          # recent mean well below history
    d = dr.assess(r, len(r) - 1, history_days=441)
    assert d["flagged"] and d["cusum"] >= dr.H
    assert d["recent_sharpe"] < d["history_sharpe"]


def test_a_stationary_rule_is_rarely_flagged():
    flagged = sum(dr.assess(_returns(441, 63, 0.0, seed), 503, history_days=441)["flagged"]
                  for seed in range(200))
    assert flagged <= 10                            # under 5% false alarms on 200 series


def test_too_little_history_or_a_rule_that_never_traded_is_not_assessed():
    short = dr.assess(np.zeros(100), 99, history_days=30)
    assert short["flagged"] is False and short["note"]
    flat = dr.assess(np.zeros(600), 599, history_days=441)
    assert flat["flagged"] is False and "no variation" in flat["note"]


def test_every_vote_carries_its_drift_assessment():
    wf = sg.walk_forward(BARS, NSE)
    ev = sg.signal_evidence(wf, "X.NS", NSE)
    assert ev["ok"]
    for v in ev["votes"]:
        assert set(v["drift"]) >= {"flagged", "cusum", "recent_sharpe", "history_sharpe"}


def test_down_weighting_is_off_by_default_and_halves_flagged_rules_when_on():
    off = sg.walk_forward(BARS, NSE)
    default = sg.walk_forward(BARS, NSE, drift_downweight=False)
    np.testing.assert_array_equal(off.score[off.first:], default.score[default.first:])
    on = sg.walk_forward(BARS, NSE, drift_downweight=True)
    ev_on = sg.signal_evidence(on, "X.NS", NSE)
    for v in ev_on["votes"]:
        expected = 0.5 if v["drift"]["flagged"] else 1.0
        assert v["drift_multiplier"] == expected
    # Same rules, same raw weights: only the multiplier differs.
    ev_off = sg.signal_evidence(off, "X.NS", NSE)
    assert {v["strategy"]: v["weight"] for v in ev_on["votes"]} == \
        {v["strategy"]: v["weight"] for v in ev_off["votes"]}


def test_drift_uses_only_returns_up_to_the_refit_day():
    full = sg.walk_forward(BARS, NSE, drift_downweight=True)
    part = sg.walk_forward(BARS[:900], NSE, drift_downweight=True)
    np.testing.assert_array_equal(part.score, full.score[:900])
