"""The regime detector, the walk-forward vote, and its evaluation.

Synthetic prices only: no network, no database. The central property is
causality. The signal on day t must be the same whether or not the bars
after t exist; the tests cut the history at several points, including
across the limited-history threshold, and compare.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from market import regime as rg
from market import signals as sg
from market.markets import NSE, US
from market.providers import Bar
from market.signal_eval import evaluate
from market.backtest import adjusted_prices
from market.strategies import STRATEGIES


def _walk(n, seed=7, drift=0.0004, vol=0.015, start=date(2018, 1, 1)):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    out, day = [], start
    for c in close:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        spread = abs(rng.normal(0, vol / 2)) * c
        o = c * (1 + rng.normal(0, vol / 4))
        out.append(Bar(day=day, open=o, high=max(o, c) + spread, low=min(o, c) - spread,
                       close=c, adj_close=c, volume=int(rng.integers(1e5, 1e6))))
        day += timedelta(days=1)
    return out


BARS = _walk(1100)


# --- regime ------------------------------------------------------------------

def _arrays(bars):
    return ([b.high for b in bars], [b.low for b in bars], [b.close for b in bars])


def test_regime_measurements_never_use_later_bars():
    full = rg.measure(*_arrays(BARS))
    for cut in (300, 640):
        part = rg.measure(*_arrays(BARS[:cut]))
        for key in full:
            np.testing.assert_array_equal(part[key], full[key][:cut], err_msg=key)


def test_a_steady_rise_is_trending_up_and_a_shock_is_volatile():
    up = _walk(400, drift=0.004, vol=0.004)
    labels = rg.classify(rg.measure(*_arrays(up)))
    assert labels[:200].count("unknown") > 0
    assert labels[-1] == "trending_up"
    calm = [Bar(day=b.day, open=b.open, high=b.high, low=b.low, close=b.close,
                adj_close=b.adj_close, volume=b.volume) for b in _walk(400, vol=0.005)]
    shocked = calm[:380] + _walk(20, seed=3, vol=0.08, start=calm[380].day)
    shocked = [Bar(day=b.day, open=b.open * calm[379].close / 100, high=b.high * calm[379].close / 100,
                   low=b.low * calm[379].close / 100, close=b.close * calm[379].close / 100,
                   adj_close=b.close * calm[379].close / 100, volume=b.volume) if i >= 380 else b
               for i, b in enumerate(shocked)]
    assert rg.classify(rg.measure(*_arrays(shocked)))[-1] == "volatile"


def test_a_flat_noisy_market_is_mostly_ranging():
    flat = _walk(700, drift=0.0, vol=0.01, seed=11)
    labels = rg.classify(rg.measure(*_arrays(flat)))[300:]
    assert labels.count("ranging") > labels.count("trending_up")


# --- the strategy tables -------------------------------------------------------

def test_every_voting_strategy_has_a_style_and_a_warmup():
    voters = set(STRATEGIES) - {"buy_and_hold"}
    assert set(sg.STYLE) == voters and set(sg.WARMUP) == voters


@pytest.mark.parametrize("key", sorted(set(STRATEGIES) - {"buy_and_hold"}))
def test_no_strategy_holds_a_position_before_its_declared_warmup(key):
    for seed in (1, 2, 3):
        pos = STRATEGIES[key].positions(adjusted_prices(_walk(600, seed=seed, drift=0.002)))
        assert not np.any(pos[:sg.WARMUP[key] - 1]), key


def test_styles_agree_with_the_cards():
    from rag.strategy_cards import load_cards
    regime_of = {c.key: c.regime for c in load_cards() if c.kind == "equity"}
    for key, style in sg.STYLE.items():
        assert (style == "trend") == (regime_of[key] == "trending"), key
        assert (style == "reversion") == (regime_of[key] == "ranging"), key


# --- the walk-forward vote -------------------------------------------------------

def test_the_signal_on_a_day_does_not_depend_on_later_bars():
    full = sg.walk_forward(BARS, NSE)
    for cut in (500, 760, 900):     # 760 crosses the limited-history threshold (750)
        part = sg.walk_forward(BARS[:cut], NSE)
        assert part.first == full.first
        np.testing.assert_array_equal(part.score[:cut], full.score[:cut])
        assert part.decision == full.decision[:cut]
        np.testing.assert_array_equal(part.position, full.position[:cut])


def test_weights_come_only_from_the_training_window_and_are_floored_at_zero():
    returns = {"ema_cross": np.zeros(800), "macd_cross": np.zeros(800)}
    returns["ema_cross"][100:200] = 0.01                 # before the window: ignored
    rng = np.random.default_rng(0)
    returns["macd_cross"][-504:] = rng.normal(-0.002, 0.01, 504)
    fit = sg._refit(799, date(2026, 1, 1), ["ema_cross", "macd_cross"], returns)
    assert fit.train_days == 504 and not fit.limited
    assert fit.weights["ema_cross"]["weight"] == 0.0
    assert fit.weights["macd_cross"]["train_sharpe"] < 0
    assert fit.weights["macd_cross"]["weight"] == 0.0


def test_decisions_follow_the_thresholds_and_hold_keeps_the_position():
    wf = sg.walk_forward(BARS, NSE)
    for t in range(wf.first, len(BARS)):
        s, d = wf.score[t], wf.decision[t]
        assert d == ("BUY" if s >= sg.BUY_AT else "SELL" if s <= sg.SELL_AT else "HOLD")
        if d == "HOLD" and t > wf.first:
            assert wf.position[t] == wf.position[t - 1]
        assert -1.0 <= s <= 1.0


def test_nse_cannot_vote_short_and_us_can():
    nse = sg.walk_forward(BARS[:700], NSE)
    assert "sma_cross_long_short" in nse.excluded
    assert all(np.all(v >= 0) for v in nse.targets.values())
    us = sg.walk_forward(BARS[:700], US)
    assert "sma_cross_long_short" in us.candidates


def test_limited_history_trains_short_skips_slow_rules_and_lowers_confidence():
    short = BARS[:546]
    wf = sg.walk_forward(short, NSE)
    ev = sg.signal_evidence(wf, "PREMIERENE.NS", NSE)
    assert ev["ok"] and ev["history"]["limited"] and ev["history"]["train_days"] == 126
    assert ev["confidence_uncapped"] == pytest.approx(abs(ev["score"]) * 126 / 504, abs=1e-4)
    assert ev["confidence"] == pytest.approx(
        min(ev["confidence_uncapped"], ev["coverage"]["fraction"]), abs=1e-4)
    for slow in ("sma_cross", "momentum", "trend_alignment", "price_above_sma"):
        assert "limited history" in ev["skipped_strategies"][slow]
    assert all(v["strategy"] not in ("momentum", "sma_cross") for v in ev["votes"])
    assert any(w.startswith("Limited history") for w in ev["warnings"])


def test_coverage_is_shown_and_caps_confidence_without_changing_the_decision():
    wf = sg.walk_forward(BARS, NSE)
    seen_narrow = False
    for t in range(wf.first, len(BARS), 7):
        ev = sg.signal_evidence(wf, "X.NS", NSE, t)
        c = ev["coverage"]
        assert c["rules_counted"] == sum(1 for v in ev["votes"] if v["weight"] > 0)
        assert c["rules_eligible"] == len(ev["votes"])
        assert c["text"] == f"{c['rules_counted']} of {c['rules_eligible']} rules"
        assert ev["confidence"] <= c["fraction"] + 1e-9
        assert ev["confidence"] <= ev["confidence_uncapped"] + 1e-9
        assert ev["signal"] == wf.decision[t]          # the decision rule is untouched
        narrow = any(w.startswith("Narrow vote") for w in ev["warnings"])
        assert narrow == (c["fraction"] < sg.MIN_COVERAGE)
        seen_narrow |= narrow
    assert seen_narrow, "the synthetic walk should produce at least one narrow vote"


def test_a_three_of_eleven_vote_cannot_read_above_0_27():
    fit = sg.Fit(on=date(2026, 1, 1), bars=1000, limited=False, train_days=504,
                 weights={k: {"train_sharpe": (1.0 if i < 3 else -1.0),
                              "weight": (1.0 if i < 3 else 0.0)}
                          for i, k in enumerate(list(sg.STYLE)[:11])},
                 skipped={})
    n = 1000
    wf = sg.WalkForward(
        days=[date(2020, 1, 1) + timedelta(days=i) for i in range(n)],
        candidates=list(fit.weights), excluded={}, first=n - 1,
        score=np.r_[np.full(n - 1, np.nan), 1.0], decision=[None] * (n - 1) + ["BUY"],
        position=np.zeros(n), targets={k: np.ones(n) for k in fit.weights}, fits=[fit],
        regime=["transition"] * n,
        measures={k: np.zeros(n) for k in ("adx", "plus_di", "minus_di", "sma50_slope_20d",
                                           "volatility", "volatility_percentile")})
    ev = sg.signal_evidence(wf, "X.NS", NSE)
    assert ev["signal"] == "BUY" and ev["score"] == 1.0
    assert ev["coverage"]["text"] == "3 of 11 rules"
    assert ev["confidence_uncapped"] == 1.0 and ev["confidence"] == pytest.approx(3 / 11, abs=1e-4)


def test_evidence_is_complete_and_adds_up():
    wf = sg.walk_forward(BARS, NSE)
    ev = sg.signal_evidence(wf, "SBIN.NS", NSE)
    assert ev["ok"] and ev["signal"] in ("BUY", "SELL", "HOLD")
    assert ev["disclaimer"] == "Educational project, not investment advice."
    assert ev["currency"] == "INR" and not ev["history"]["limited"]
    shares = [v["share_of_vote"] for v in ev["votes"]]
    if sum(v["weight"] for v in ev["votes"]) > 0:
        assert sum(shares) == pytest.approx(1.0, abs=1e-3)
        recomputed = sum(v["share_of_vote"] * v["vote"] for v in ev["votes"])
        assert recomputed == pytest.approx(ev["score"], abs=1e-3)
    assert all(v["card"].startswith("docs/strategies/") for v in ev["votes"])
    assert set(ev["regime"]) >= {"label", "adx", "sma50_slope_20d", "volatility_percentile"}


def test_too_little_history_says_so_instead_of_signalling():
    wf = sg.walk_forward(BARS[:100], NSE)
    ev = sg.signal_evidence(wf, "NEW.NS", NSE)
    assert not ev["ok"] and "Not enough history" in ev["error"]


# --- evaluation --------------------------------------------------------------------

def test_the_evaluation_is_out_of_sample_reproducible_and_complete():
    nifty = _walk(1100, seed=99)
    a = evaluate(BARS, nifty, NSE, years=2, n_random=20)
    b = evaluate(BARS, nifty, NSE, years=2, n_random=20)
    assert a == b
    assert a["ok"] and a["random"]["runs"] == 20
    wf = sg.walk_forward(BARS, NSE)
    assert a["start"] >= wf.days[wf.first].isoformat()
    assert sum(a["decisions"].values()) == a["trading_days"]
    for side in ("signal", "buy_and_hold"):
        assert {"cagr", "sharpe_ratio", "max_drawdown", "costs_paid"} <= set(a[side])
    assert a["random"]["sharpe_p5"] <= a["random"]["sharpe_median"] <= a["random"]["sharpe_p95"]
    assert 0 <= a["hit_rate"]["base_rate_up"] <= 1


def test_risk_metrics_match_their_definitions():
    import pandas as pd

    from market.signal_eval import risk_metrics
    rng = np.random.default_rng(5)
    eq = 100 * np.cumprod(1 + rng.normal(0.0005, 0.012, 600))
    held = (rng.random(600) > 0.4).astype(float)
    m = risk_metrics(eq, held)
    r = pd.Series(eq).pct_change().dropna()
    assert m["sharpe"] == pytest.approx(r.mean() / r.std() * np.sqrt(252), abs=1e-4)
    downside = np.sqrt((r.clip(upper=0) ** 2).mean()) * np.sqrt(252)
    assert m["sortino"] == pytest.approx(r.mean() * 252 / downside, abs=1e-4)
    dd = (pd.Series(eq) / pd.Series(eq).cummax() - 1).min()
    cagr = (eq[-1] / eq[0]) ** (252 / 599) - 1
    assert m["max_drawdown"] == pytest.approx(dd, abs=1e-4)
    assert m["calmar"] == pytest.approx(cagr / abs(dd), abs=1e-4)
    assert m["time_in_market"] == pytest.approx(np.mean(held[1:] > 0), abs=1e-4)
    assert risk_metrics(eq)["time_in_market"] == 1.0
    assert risk_metrics([100.0] * 10)["sharpe"] is None


def test_the_evaluation_reports_risk_for_the_signal_and_both_benchmarks():
    out = evaluate(BARS, _walk(1100, seed=99), NSE, years=2, n_random=5)
    for side in ("signal", "buy_and_hold", "nifty"):
        assert set(out["risk"][side]) == {"cagr", "max_drawdown", "sharpe", "sortino",
                                          "calmar", "time_in_market"}
    assert out["risk"]["buy_and_hold"]["time_in_market"] == 1.0
    assert out["risk"]["signal"]["time_in_market"] == pytest.approx(
        out["signal"]["exposure"], abs=1e-4)


# --- the regime direction rule ----------------------------------------------------

def _one_day(adx, slope, plus, minus, pct=0.5):
    return {k: np.array([v], dtype=float) for k, v in {
        "adx": adx, "sma50_slope_20d": slope, "plus_di": plus, "minus_di": minus,
        "volatility_percentile": pct}.items()}


def test_the_agree_rule_calls_a_disagreeing_strong_trend_a_transition():
    sbin = _one_day(42.4, -0.0246, 38.27, 5.78)           # SBIN.NS on 2026-11-16
    assert rg.classify(sbin, "slope") == ["trending_down"]
    assert rg.classify(sbin, "agree") == ["transition"]
    assert rg.classify(_one_day(30, 0.02, 30, 10), "agree") == ["trending_up"]
    assert rg.classify(_one_day(30, -0.02, 10, 30), "agree") == ["trending_down"]
    assert rg.classify(_one_day(15, -0.02, 38, 5), "agree") == ["ranging"]
    with pytest.raises(ValueError, match="Known"):
        rg.classify(sbin, "vibes")


def test_the_default_rule_is_still_the_original_one():
    m = rg.measure(*_arrays(BARS))
    assert rg.DEFAULT_RULE == "slope"
    assert rg.classify(m) == rg.classify(m, "slope")
    np.testing.assert_array_equal(sg.walk_forward(BARS[:700], NSE).score,
                                  sg.walk_forward(BARS[:700], NSE, regime_rule="slope").score)


def test_the_agree_rule_is_causal_too():
    full = sg.walk_forward(BARS, NSE, regime_rule="agree")
    part = sg.walk_forward(BARS[:800], NSE, regime_rule="agree")
    np.testing.assert_array_equal(part.score, full.score[:800])


# --- the refit schedule ------------------------------------------------------------

def test_weights_are_refitted_on_the_first_trading_day_of_each_month():
    wf = sg.walk_forward(BARS, NSE)
    refits = sorted({f.on for f in wf.fits})
    assert refits[0] == wf.days[wf.first]
    for day in refits[1:]:
        i = wf.days.index(day)
        assert wf.days[i - 1].month != day.month          # the month's first bar


def test_where_the_history_starts_no_longer_moves_later_signals():
    """Fetching a few days less history used to shift every refit (it was
    every 21 bars from the first signal) and change the five-year result."""
    full = sg.walk_forward(BARS, NSE)
    for skip in (1, 5, 13):
        later = sg.walk_forward(BARS[skip:], NSE)
        # Compare well after both are past warm-up and the limited-history
        # threshold, where only the schedule could make them differ.
        for t in range(850, len(BARS)):
            assert later.decision[t - skip] == full.decision[t], (skip, t)


def test_the_refit_day_can_be_moved_to_measure_its_effect():
    a = sg.walk_forward(BARS, NSE, refit_day=0)
    b = sg.walk_forward(BARS, NSE, refit_day=10)
    days_b = sorted({f.on for f in b.fits})[1:]
    assert all(sum(1 for d in b.days[:b.days.index(x)] if (d.year, d.month) == (x.year, x.month))
               == 10 for x in days_b)
    assert sorted({f.on for f in a.fits}) != sorted({f.on for f in b.fits})


def test_a_flat_long_only_rule_votes_out_on_every_market():
    """On a US stock a flat long-only rule used to vote 0, so the score
    hardly ever reached SELL and NVDA was held for five years straight."""
    assert sg._vote(0.0, "sma_cross", US) == -1.0 and sg._vote(1.0, "sma_cross", US) == 1.0
    assert sg._vote(0.0, "sma_cross", NSE) == -1.0
    assert sg._vote(-1.0, "sma_cross_long_short", US) == -1.0
    assert sg._vote(0.0, "sma_cross_long_short", US) == 0.0


def test_a_us_stock_gets_sell_signals_when_its_rules_are_out():
    falling = _walk(1100, seed=21, drift=-0.0015, vol=0.012)
    wf = sg.walk_forward(falling, US)
    decided = [d for d in wf.decision[wf.first:] if d]
    assert decided.count("SELL") > 0
    assert wf.position[wf.first:].min() == 0.0                   # it does get out
