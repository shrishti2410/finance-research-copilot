"""market/ablation.py: the ensembles, the exposure-matched benchmark and the
metrics used for benchmarks/QUANT_HONESTY.md. Synthetic prices; no database."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from market.ablation import (
    ENSEMBLES,
    correlation,
    ensemble_positions,
    exposure_matched,
    full_metrics,
    rule_returns,
    turnover_per_year,
)
from market.costs import cost_model_for
from market.markets import NSE
from market.providers import Bar
from market.signal_eval import risk_metrics
from market.signals import walk_forward


def _bars(n=1100, seed=7):
    rng = np.random.default_rng(seed)
    out, day, price = [], date(2021, 1, 4), 100.0
    while len(out) < n:
        if day.weekday() < 5:
            # Trending stretches and ranges, so the rules disagree.
            drift = 0.0012 if (len(out) // 150) % 2 == 0 else -0.0008
            price *= float(np.exp(drift + rng.normal(0, 0.015)))
            hi, lo = price * 1.01, price * 0.99
            out.append(Bar(day=day, open=price * (1 + rng.normal(0, 0.003)), high=hi, low=lo,
                           close=price, adj_close=price, volume=1_000_000))
        day += timedelta(days=1)
    return out


@pytest.fixture(scope="module")
def setup():
    bars = _bars()
    model = cost_model_for(NSE.cost_model)
    wf = walk_forward(bars, NSE, model)
    return bars, model, wf


def test_the_regime_ensemble_is_the_production_signal(setup):
    _, _, wf = setup
    assert np.array_equal(ensemble_positions(wf, NSE, "regime"), wf.position)


@pytest.mark.parametrize("kind", ENSEMBLES)
def test_every_ensemble_is_long_or_flat_and_silent_before_the_first_signal(setup, kind):
    _, _, wf = setup
    pos = ensemble_positions(wf, NSE, kind)
    assert set(np.unique(pos)) <= {0.0, 1.0}
    assert not pos[: wf.first].any()


def test_the_ensembles_differ_from_each_other_on_these_prices(setup):
    _, _, wf = setup
    series = {k: ensemble_positions(wf, NSE, k) for k in ENSEMBLES}
    assert len({tuple(s) for s in series.values()}) >= 3


def test_majority_is_in_exactly_when_more_than_half_the_eligible_rules_are_in(setup):
    _, _, wf = setup
    pos = ensemble_positions(wf, NSE, "majority")
    for t in range(wf.first, len(wf.days), 37):
        eligible = list(wf.fits[t - wf.first].weights)
        ins = sum(1 for k in eligible if wf.targets[k][t] > 0)
        assert pos[t] == (1.0 if ins > len(eligible) / 2 else 0.0)


def test_best_rule_follows_the_rule_with_the_highest_trailing_sharpe(setup):
    _, _, wf = setup
    pos = ensemble_positions(wf, NSE, "best_rule")
    for t in range(wf.first, len(wf.days), 41):
        w = wf.fits[t - wf.first].weights
        best = max(w, key=lambda k: (w[k]["train_sharpe"], k))
        assert pos[t] == (1.0 if wf.targets[best][t] > 0 else 0.0)


def test_unknown_ensemble_is_refused(setup):
    with pytest.raises(ValueError):
        ensemble_positions(setup[2], NSE, "vibes")


def test_exposure_matched_scales_returns_and_keeps_the_sharpe():
    eq = [100, 102, 101, 104, 103, 107, 106, 110]
    half = exposure_matched(eq, 0.5)
    r = np.diff(eq) / np.asarray(eq[:-1], float)
    rh = np.diff(half) / np.asarray(half[:-1], float)
    assert half[0] == 100 and np.allclose(rh, 0.5 * r)
    assert exposure_matched(eq, 1.0) == pytest.approx(eq)
    assert risk_metrics(half)["sharpe"] == pytest.approx(risk_metrics(eq)["sharpe"], abs=1e-3)
    assert risk_metrics(half)["max_drawdown"] > risk_metrics(eq)["max_drawdown"]  # shallower


def test_turnover_counts_changes_per_year():
    assert turnover_per_year([0] * 252) == 0
    assert turnover_per_year([0, 1, 1, 0] + [0] * 248) == pytest.approx(2.0)


def test_full_metrics_has_every_preregistered_field_and_cost_drag_is_never_negative(setup):
    bars, model, wf = setup
    day0 = bars[wf.first].day
    m = full_metrics(bars, wf.position, day0, model)
    for k in ("cagr", "max_drawdown", "sharpe", "sortino", "exposure", "turnover_per_year", "cost_drag"):
        assert k in m
    assert m["cost_drag"] >= 0
    never = full_metrics(bars, np.zeros(len(bars)), day0, model)
    assert never["exposure"] == 0 and never["cost_drag"] == 0 and never["turnover_per_year"] == 0


def test_rule_correlation_is_symmetric_with_ones_on_the_diagonal(setup):
    bars, model, wf = setup
    keys, c = correlation(rule_returns(bars, wf, wf.first, model))
    assert keys == sorted(wf.candidates) and len(keys) == 16
    ok = ~np.isnan(np.diag(c))
    assert np.allclose(np.diag(c)[ok], 1.0) and np.allclose(c, c.T, equal_nan=True)


# --- the double vote ------------------------------------------------------------------

from market.ablation import agreement, clusters_at, rule_return_series, variant_positions  # noqa: E402


def test_with_no_exclusion_and_no_clusters_the_variant_is_production(setup):
    _, _, wf = setup
    assert np.array_equal(variant_positions(wf, NSE), wf.position)


def test_clusters_above_one_never_form_so_the_variant_is_production(setup):
    bars, model, wf = setup
    returns = rule_return_series(bars, wf, model)
    assert np.array_equal(variant_positions(wf, NSE, returns=returns, threshold=1.01), wf.position)


def test_excluding_a_rule_changes_only_the_vote_it_was_in(setup):
    _, _, wf = setup
    dropped = variant_positions(wf, NSE, exclude=frozenset({"zscore_reversion"}))
    assert set(np.unique(dropped)) <= {0.0, 1.0} and not dropped[: wf.first].any()


def test_identical_rules_cluster_together_under_the_higher_sharpe_leader():
    rng = np.random.default_rng(1)
    base = rng.normal(0, 0.01, 300)
    returns = {"a": base, "b": base.copy(), "c": rng.normal(0, 0.01, 300)}
    weights = {"a": {"train_sharpe": 0.5, "weight": 0.5}, "b": {"train_sharpe": 0.9, "weight": 0.9},
               "c": {"train_sharpe": 0.1, "weight": 0.1}}
    lead = clusters_at(weights, 299, 300, returns)
    assert lead == {"b": "b", "a": "b", "c": "c"}


def test_clusters_use_only_the_training_window():
    # Identical before bar 200, unrelated after: a refit at bar 199 clusters them.
    rng = np.random.default_rng(2)
    a = rng.normal(0, 0.01, 400)
    b = a.copy()
    b[200:] = rng.normal(0, 0.01, 200)
    w = {"a": {"train_sharpe": 1.0, "weight": 1.0}, "b": {"train_sharpe": 0.5, "weight": 0.5}}
    assert clusters_at(w, 199, 200, {"a": a, "b": b})["b"] == "a"
    assert clusters_at(w, 399, 200, {"a": a, "b": b})["b"] == "b"


def test_agreement_is_the_share_of_matching_days():
    assert agreement([0, 1, 1, 0], [0, 1, 0, 0]) == 0.75
