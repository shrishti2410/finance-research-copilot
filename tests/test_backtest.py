"""The strategies and the backtester.

What is pinned:
- **No look-ahead.** Every strategy, run on history cut at bar k, gives the
  same positions for bars 0..k as it does on the full history. The engine
  fills a decision made at bar t's close at bar t+1's open.
- **Costs are exact.** One round trip at a known cost loses exactly that.
- **Buy-and-hold** is computed on the same terms and returned alongside.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from market.backtest import adjusted_prices, run_backtest
from market.providers import Bar
from market.strategies import STRATEGIES, Prices, Strategy, get_strategy

RNG = np.random.default_rng(7)
N = 700


def make_bars(n=N, dividend_factor=False):
    close = 50 * np.exp(np.cumsum(RNG.normal(0.0003, 0.02, n)))
    opens = close * (1 + RNG.normal(0, 0.004, n))
    high = np.maximum(opens, close) * (1 + np.abs(RNG.normal(0, 0.006, n)))
    low = np.minimum(opens, close) * (1 - np.abs(RNG.normal(0, 0.006, n)))
    adj = close * (np.linspace(0.9, 1.0, n) if dividend_factor else 1.0)
    start = date(2020, 1, 1)
    return [Bar(day=start + timedelta(days=i), open=float(opens[i]), high=float(high[i]),
                low=float(low[i]), close=float(close[i]), adj_close=float(adj[i]),
                volume=1_000_000) for i in range(n)]


BARS = make_bars()


def test_there_are_twelve_classic_strategies_and_buy_and_hold_is_one():
    assert len(STRATEGIES) >= 12 and "buy_and_hold" in STRATEGIES
    for s in STRATEGIES.values():
        assert s.name and s.description and isinstance(s.params, dict)
    with pytest.raises(KeyError, match="Known"):
        get_strategy("astrology")


@pytest.mark.parametrize("key", sorted(STRATEGIES))
def test_no_strategy_looks_ahead(key):
    """Positions up to bar k do not change when bars after k are added."""
    strategy = STRATEGIES[key]
    full = strategy.positions(adjusted_prices(BARS))
    for k in (30, 150, 260, 400, 699):
        cut = strategy.positions(adjusted_prices(BARS[: k + 1]))
        np.testing.assert_array_equal(cut, full[: k + 1], err_msg=f"{key} at bar {k}")


@pytest.mark.parametrize("key", sorted(STRATEGIES))
def test_every_strategy_backtests_and_stays_in_bounds(key):
    result = run_backtest(BARS, key)
    positions = np.array(result.positions)
    low = -1.0 if STRATEGIES[key].allows_short else 0.0
    assert positions.min() >= low and positions.max() <= 1.0
    assert len(result.equity) == N and result.equity[0] == 10_000.0
    assert result.stats["final_equity"] > 0


def _fixed(signal):
    return Strategy("fixed", "Fixed", "Positions given in advance.", {},
                    lambda p: np.asarray(signal, dtype=float), allows_short=True)


def _bars(opens, closes):
    start = date(2024, 1, 1)
    return [Bar(day=start + timedelta(days=i), open=o, high=max(o, c), low=min(o, c),
                close=c, adj_close=c, volume=1) for i, (o, c) in enumerate(zip(opens, closes))]


def test_a_decision_at_a_close_is_filled_at_the_next_open():
    bars = _bars([10, 11, 12, 13, 14], [10.5, 11.5, 12.5, 13.5, 14.5])
    result = run_backtest(bars, _fixed([0, 1, 1, 0, 0]), cost_bps=0, slippage_bps=10)
    trade = result.trades[0]
    assert trade.entry_day == bars[2].day.isoformat(), "signal at bar 1, filled at bar 2"
    assert trade.entry_price == pytest.approx(12 * 1.001), "the open, plus slippage"
    assert trade.exit_day == bars[4].day.isoformat()
    assert trade.exit_price == pytest.approx(14 * 0.999)
    assert result.positions == [0, 0, 1, 1, 0]


def test_without_costs_always_long_earns_from_the_first_fill_to_the_last_close():
    bars = make_bars(50)
    result = run_backtest(bars, "buy_and_hold", cost_bps=0, slippage_bps=0)
    p = adjusted_prices(bars)
    # Equity is reported to four decimals: equal to well under a cent on 10,000.
    assert result.equity[-1] == pytest.approx(10_000 * p.close[-1] / p.open[1], rel=1e-7)
    assert result.benchmark_equity == result.equity


def test_a_round_trip_pays_its_costs_exactly():
    bars = _bars([10] * 6, [10] * 6)                 # flat prices: only costs move equity
    result = run_backtest(bars, _fixed([1, 1, 0, 0, 0, 0]), cost_bps=7, slippage_bps=3)
    rate = (7 + 3) / 1e4
    assert result.equity[-1] == pytest.approx(10_000 * (1 - rate) * (1 - rate), rel=1e-12)
    assert result.stats["costs_paid"] == pytest.approx(10_000 - result.equity[-1], rel=1e-9)
    assert result.stats["turnover"] == 2.0


def test_a_flip_from_long_to_short_trades_twice_the_equity_and_closes_the_trade():
    bars = _bars([10, 10, 10, 10, 10], [10, 10, 10, 10, 10])
    result = run_backtest(bars, _fixed([1, -1, -1, -1, -1]), cost_bps=10, slippage_bps=0)
    assert result.stats["turnover"] == 3.0
    assert [t.side for t in result.trades] == ["long", "short"]
    assert result.trades[0].status == "closed" and result.trades[1].status == "open"


def test_a_short_earns_when_the_price_falls():
    bars = _bars([10, 10, 9, 8], [10, 9.5, 8.5, 7.5])
    result = run_backtest(bars, _fixed([-1, -1, -1, -1]), cost_bps=0, slippage_bps=0)
    assert result.equity[-1] > 10_000


def test_returns_include_dividends_through_the_adjusted_prices():
    bars = make_bars(300, dividend_factor=True)
    p = adjusted_prices(bars)
    raw_growth = bars[-1].close / bars[1].open
    result = run_backtest(bars, "buy_and_hold", cost_bps=0, slippage_bps=0)
    assert result.equity[-1] / 10_000 == pytest.approx(p.close[-1] / p.open[1])
    assert result.equity[-1] / 10_000 > raw_growth, "the adjustment adds the dividends back"


def test_the_benchmark_comparison_is_on_the_same_terms():
    result = run_backtest(BARS, "sma_cross", cost_bps=5, slippage_bps=5)
    assert len(result.benchmark_equity) == len(result.equity)
    assert result.benchmark_stats["trades"] == 1
    assert result.benchmark_stats["costs_paid"] > 0


def test_parameters_override_defaults_and_unknown_ones_are_ignored():
    result = run_backtest(BARS, "sma_cross", params={"fast": 20, "slow": 100, "nonsense": 3})
    assert result.params == {"fast": 20.0, "slow": 100.0}


def test_bad_inputs_are_refused():
    with pytest.raises(ValueError):
        run_backtest(BARS[:2], "buy_and_hold")
    with pytest.raises(ValueError):
        run_backtest(BARS, "buy_and_hold", cost_bps=-1)


def test_prices_wrapper_reports_its_length():
    assert len(Prices(*(np.zeros(4) for _ in range(5)))) == 4


def test_a_warm_up_lets_a_slow_strategy_trade_from_the_first_day():
    """SMA 50/200 needs 200 bars; given them before start_day it can be long
    on the window's first bar instead of sitting flat for most of a year."""
    start = BARS[300].day
    result = run_backtest(BARS, "price_above_sma", start_day=start)
    assert result.days[0] == start.isoformat() and len(result.days) == N - 300
    strategy = STRATEGIES["price_above_sma"]
    full = strategy.positions(adjusted_prices(BARS))
    # The first trade is decided by the warm-up's last position, not by a blank one.
    assert result.positions[1] == full[300]
    assert result.benchmark_equity[0] == 10_000.0


def test_too_few_bars_after_the_start_are_refused():
    with pytest.raises(ValueError, match="three bars"):
        run_backtest(BARS, "buy_and_hold", start_day=BARS[-2].day)
