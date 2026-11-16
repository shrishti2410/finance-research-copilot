"""Indicators and analytics, each checked against an independent calculation.

market/indicators.py is written in NumPy loops and cumulative sums; the
references here use pandas' rolling and ewm machinery (or the textbook
formula written out), so an error would have to be made twice, in two
different ways, to pass. The price series is synthetic but realistic: a
random walk with gaps, a flat stretch and a run of rises, seeded so it is the
same every time.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from market import analytics as an
from market import indicators as ind
from market.providers import Bar

RNG = np.random.default_rng(20261116)
N = 400
_close = 100 * np.exp(np.cumsum(RNG.normal(0.0004, 0.018, N)))
_close[150:160] = _close[149]                      # flat: RSI with no gains or losses
_close[300:315] = _close[299] * np.cumprod(np.full(15, 1.01))   # a run of rises
CLOSE = pd.Series(_close)
HIGH = CLOSE * (1 + np.abs(RNG.normal(0, 0.01, N)))
LOW = CLOSE * (1 - np.abs(RNG.normal(0, 0.01, N)))
VOLUME = pd.Series(RNG.integers(1_000_000, 9_000_000, N).astype(float))


def same(ours, theirs, rel=1e-9, abs_=1e-9):
    ours, theirs = np.asarray(ours, float), np.asarray(theirs, float)
    assert ours.shape == theirs.shape
    assert np.array_equal(np.isnan(ours), np.isnan(theirs)), "warm-up NaNs differ"
    mask = ~np.isnan(ours)
    np.testing.assert_allclose(ours[mask], theirs[mask], rtol=rel, atol=abs_)


@pytest.mark.parametrize("n", [5, 20, 50, 200])
def test_sma_matches_pandas_rolling_mean(n):
    same(ind.sma(CLOSE, n), CLOSE.rolling(n).mean())


@pytest.mark.parametrize("n", [12, 26, 50])
def test_ema_matches_pandas_ewm(n):
    same(ind.ema(CLOSE, n), CLOSE.ewm(span=n, adjust=False, min_periods=n).mean())


def test_rsi_matches_pandas_wilder():
    delta = CLOSE.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    reference = 100 - 100 / (1 + gain / loss)
    reference[(loss == 0) & (gain > 0)] = 100.0
    same(ind.rsi(CLOSE, 14), reference)


def test_rsi_is_bounded_and_pinned_by_a_run_of_rises():
    values = ind.rsi(CLOSE, 14)
    assert np.nanmin(values) >= 0 and np.nanmax(values) <= 100
    assert values[314] > 70, "fifteen straight 1% rises cross the usual overbought line"
    assert np.all(np.diff(values[300:315]) > 0), "and RSI rises on every one of them"
    assert ind.rsi([1, 2, 3, 4, 5, 6], 3)[-1] == 100.0


def test_macd_matches_pandas():
    fast = CLOSE.ewm(span=12, adjust=False, min_periods=12).mean()
    slow = CLOSE.ewm(span=26, adjust=False, min_periods=26).mean()
    line = fast - slow
    signal = line.ewm(span=9, adjust=False, min_periods=9).mean()
    ours = ind.macd(CLOSE)
    same(ours[0], line)
    same(ours[1], signal)
    same(ours[2], line - signal)


def test_bollinger_matches_pandas_population_std():
    mid = CLOSE.rolling(20).mean()
    std = CLOSE.rolling(20).std(ddof=0)
    middle, upper, lower = ind.bollinger(CLOSE, 20, 2.0)
    same(middle, mid)
    same(upper, mid + 2 * std, rel=1e-8)
    same(lower, mid - 2 * std, rel=1e-8)


def test_atr_matches_pandas():
    prev = CLOSE.shift(1)
    tr = pd.concat([HIGH - LOW, (HIGH - prev).abs(), (LOW - prev).abs()], axis=1).max(axis=1)
    same(ind.true_range(HIGH, LOW, CLOSE), tr)
    same(ind.atr(HIGH, LOW, CLOSE, 14), tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())


def test_volume_indicators_match_pandas():
    same(ind.volume_sma(VOLUME, 20), VOLUME.rolling(20).mean())
    direction = np.sign(CLOSE.diff()).fillna(0)
    same(ind.obv(CLOSE, VOLUME), (direction * VOLUME).cumsum())


def test_too_short_a_series_is_all_warm_up():
    assert np.isnan(ind.sma([1, 2], 5)).all() and np.isnan(ind.rsi([1, 2, 3], 14)).all()
    assert np.isnan(ind.ema([], 5)).all()


# ── analytics ─────────────────────────────────────────────────────────────────

DAYS = [date(2025, 1, 1) + timedelta(days=i) for i in range(N)]
ADJ = CLOSE.to_numpy() * 0.97                  # a different series from the traded close


def bars(adj=ADJ, days=DAYS):
    return [Bar(day=d, open=c, high=c, low=c, close=c / 0.97, adj_close=c, volume=1)
            for d, c in zip(days, adj)]


def test_returns_and_volatility_match_pandas():
    s = pd.Series(ADJ)
    same(an.simple_returns(ADJ), s.pct_change().dropna())
    same(an.log_returns(ADJ), np.log(s).diff().dropna())
    assert an.volatility(an.simple_returns(ADJ)) == pytest.approx(
        s.pct_change().std(ddof=1) * math.sqrt(252), rel=1e-12)


def test_max_drawdown_matches_a_pandas_running_peak():
    s = pd.Series(ADJ, index=DAYS)
    dd = s / s.cummax() - 1
    ours = an.max_drawdown(ADJ, DAYS)
    assert ours.max_drawdown == pytest.approx(dd.min(), rel=1e-12)
    assert ours.trough_day == dd.idxmin()
    assert ours.peak_day == s.loc[:dd.idxmin()].idxmax()


def test_beta_and_correlation_match_pandas_on_the_common_days():
    bench = 50 * np.exp(np.cumsum(RNG.normal(0.0003, 0.012, N)))
    asset = {d: p for d, p in zip(DAYS, ADJ)}
    benchmark = {d: p for i, (d, p) in enumerate(zip(DAYS, bench)) if i % 7 != 3}  # gaps
    beta, corr, n = an.beta_and_correlation(asset, benchmark)
    frame = pd.DataFrame({"a": pd.Series(asset), "b": pd.Series(benchmark)}).dropna()
    returns = frame.pct_change().dropna()
    assert n == len(returns)
    assert beta == pytest.approx(returns.cov().loc["a", "b"] / returns["b"].var(), rel=1e-10)
    assert corr == pytest.approx(returns.corr().loc["a", "b"], rel=1e-10)


def test_a_series_against_itself_has_beta_and_correlation_one():
    asset = {d: p for d, p in zip(DAYS, ADJ)}
    beta, corr, _ = an.beta_and_correlation(asset, asset)
    assert beta == pytest.approx(1.0) and corr == pytest.approx(1.0)


def test_summary_uses_the_adjusted_close_and_states_it():
    out = an.summary(bars())
    assert out["total_return"] == pytest.approx(ADJ[-1] / ADJ[0] - 1, rel=1e-6)
    assert out["price_basis"] == "dividend-adjusted close"
    assert out["trading_days"] == N and out["max_drawdown"] < 0
    expected = (ADJ[-1] / ADJ[0]) ** (252 / (N - 1)) - 1
    assert out["annualised_return"] == pytest.approx(expected, rel=1e-6)


def test_summary_with_a_benchmark_reports_beta_and_needs_two_bars():
    out = an.summary(bars(), bars())
    assert out["beta"] == pytest.approx(1.0) and out["overlapping_return_days"] == N - 1
    with pytest.raises(ValueError):
        an.summary(bars()[:1])


def test_compute_names_every_series_and_refuses_unknown_names():
    sample = bars()
    for name in ind.INDICATORS:
        series = ind.compute(name, sample)
        assert series and all(len(v) == len(sample) for v in series.values())
    assert set(ind.compute("macd", sample)) == {"macd", "macd_signal", "macd_hist"}
    with pytest.raises(KeyError, match="Known"):
        ind.compute("stochastic", sample)
