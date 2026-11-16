"""NSE tickers, currencies, and the Indian equity-delivery cost model.

The charges are checked against hand arithmetic on a ₹1,00,000 trade at the
rates in market/costs.py (Zerodha's published equity-delivery column, read
2026-11-16).
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from market.backtest import run_backtest
from market.costs import FlatBps, IndiaEquityDelivery, cost_model_for
from market.markets import market_for, normalize_ticker
from market.providers import Bar
from market.strategies import Strategy


@pytest.mark.parametrize("raw, symbol", [
    ("COFORGE.NS", "COFORGE.NS"), ("sbin.ns", "SBIN.NS"), (" ^nsei ", "^NSEI"),
    ("brk.b", "BRK-B"), ("NVDA", "NVDA"), ("RELIANCE.BO", "RELIANCE.BO"),
])
def test_exchange_suffixes_and_indices_survive_normalising(raw, symbol):
    assert normalize_ticker(raw) == symbol


def test_an_nse_stock_and_the_nifty_are_in_rupees_with_indian_costs():
    for ticker in ("COFORGE.NS", "PREMIERENE.NS", "^NSEI"):
        m = market_for(ticker)
        assert (m.exchange, m.currency, m.currency_symbol) == ("NSE", "INR", "₹")
        assert m.cost_model == "india_equity_delivery" and not m.allows_overnight_short
    us = market_for("NVDA")
    assert (us.exchange, us.currency, us.cost_model) == ("US", "USD", "flat_bps")


def test_a_one_lakh_delivery_buy_is_charged_as_the_schedule_says():
    c = IndiaEquityDelivery().charges("buy", 100_000)
    assert c.items["stt"] == pytest.approx(100.0)           # 0.1%
    assert c.items["exchange"] == pytest.approx(3.07)       # 0.00307%
    assert c.items["sebi"] == pytest.approx(0.10)           # ₹10 per crore
    assert c.items["stamp_duty"] == pytest.approx(15.0)     # 0.015%, buys only
    assert c.items["gst"] == pytest.approx(0.18 * (0 + 3.07 + 0.10))
    assert c.items["dp_charge"] == 0.0 and c.items["brokerage"] == 0.0
    assert c.total == pytest.approx(100 + 3.07 + 0.10 + 15 + 0.5706)


def test_a_one_lakh_delivery_sale_pays_the_dp_charge_and_no_stamp_duty():
    c = IndiaEquityDelivery().charges("sell", 100_000)
    assert c.items["stamp_duty"] == 0.0 and c.items["dp_charge"] == pytest.approx(15.34)
    assert c.total == pytest.approx(100 + 3.07 + 0.10 + 0.5706 + 15.34)


def test_a_brokerage_cap_applies_when_set():
    c = IndiaEquityDelivery(brokerage_rate=0.001, brokerage_cap=20.0).charges("buy", 100_000)
    assert c.items["brokerage"] == 20.0
    assert c.items["gst"] == pytest.approx(0.18 * (20 + 3.07 + 0.10))


def test_cost_models_by_name():
    assert isinstance(cost_model_for("india_equity_delivery"), IndiaEquityDelivery)
    assert cost_model_for("flat_bps", 7).cost_bps == 7
    with pytest.raises(KeyError, match="Known"):
        cost_model_for("free")


def _bars(prices):
    start = date(2025, 1, 1)
    return [Bar(day=start + timedelta(days=i), open=p, high=p, low=p, close=p,
                adj_close=p, volume=1) for i, p in enumerate(prices)]


def _fixed(signal, short=False):
    return Strategy("fixed", "Fixed", "Positions given in advance.", {},
                    lambda p: np.asarray(signal, dtype=float), allows_short=short)


def test_a_round_trip_in_rupees_pays_both_legs_of_the_schedule():
    model = IndiaEquityDelivery()
    result = run_backtest(_bars([500.0] * 6), _fixed([1, 1, 0, 0, 0, 0]),
                          initial_cash=100_000, slippage_bps=0, cost_model=model)
    buy = model.charges("buy", 100_000).total
    sell = model.charges("sell", 100_000 - buy).total
    assert result.equity[-1] == pytest.approx(100_000 - buy - sell, abs=1e-3)
    assert result.stats["costs_paid"] == pytest.approx(buy + sell, rel=1e-6)
    trade = result.trades[0]
    assert trade.net_return == pytest.approx(-(buy + sell) / 100_000, rel=1e-3)


def test_indian_delivery_cannot_hold_a_short_overnight():
    result = run_backtest(_bars([100.0, 99, 98, 97, 96]), _fixed([-1] * 5, short=True),
                          initial_cash=100_000, slippage_bps=0,
                          cost_model=IndiaEquityDelivery())
    assert set(result.positions) == {0.0} and result.trades == []


def test_the_flat_model_is_what_cost_bps_always_meant():
    bars = _bars([10.0] * 6)
    signal = _fixed([1, 1, 0, 0, 0, 0])
    old_style = run_backtest(bars, signal, cost_bps=7, slippage_bps=3)
    explicit = run_backtest(bars, signal, slippage_bps=3, cost_model=FlatBps(7))
    assert old_style.equity == explicit.equity


# --- the service: currency, the market's own index, the cost model by market ---

import asyncio  # noqa: E402

from market import service  # noqa: E402


class _Recorder:
    """A MarketData stand-in that returns flat prices and records what was asked."""

    def __init__(self):
        self.asked = []
        self.provider = type("P", (), {"name": "fake"})()

    async def history(self, ticker, start, end):
        self.asked.append(ticker)
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return [Bar(day=d, open=100.0 + i % 3, high=101.0 + i % 3, low=99.0, close=100.0 + i % 3,
                    adj_close=100.0 + i % 3, volume=1) for i, d in enumerate(days)]


def test_an_nse_stock_is_compared_with_the_nifty_and_reported_in_rupees():
    data = _Recorder()
    out = asyncio.run(service.price_analytics("sbin.ns", date(2026, 1, 1), date(2026, 3, 1),
                                              data=data))
    assert data.asked == ["SBIN.NS", "^NSEI"]
    assert (out["benchmark"], out["currency"], out["currency_symbol"]) == ("^NSEI", "INR", "₹")
    us = _Recorder()
    asyncio.run(service.price_analytics("NVDA", date(2026, 1, 1), date(2026, 3, 1), data=us))
    assert us.asked == ["NVDA", "SPY"]
    none = _Recorder()
    asyncio.run(service.price_analytics("NVDA", date(2026, 1, 1), date(2026, 3, 1), None, data=none))
    assert none.asked == ["NVDA"]


def test_an_nse_backtest_is_charged_indian_delivery_costs_and_never_shorts():
    out = asyncio.run(service.backtest("DIXON.NS", "sma_cross_long_short", date(2026, 1, 1),
                                       date(2026, 6, 30), data=_Recorder()))
    assert out["currency"] == "INR" and out["costs"]["model"] == "india_equity_delivery"
    assert min(out["positions"]) >= 0
    us = asyncio.run(service.backtest("NVDA", "macd_cross", date(2026, 1, 1),
                                      date(2026, 6, 30), cost_bps=7, data=_Recorder()))
    assert us["costs"] == {"model": "flat_bps", "cost_bps_per_side": 7}


def test_history_is_measured_and_a_short_one_is_flagged():
    from market.universe import FULL_HISTORY_TRADING_DAYS, history_of
    short = history_of(_bars([1.0] * 546))
    assert short.trading_days == 546 and short.limited and short.largest_gap_days == 1
    full = history_of(_bars([1.0] * FULL_HISTORY_TRADING_DAYS))
    assert not full.limited and full.years == pytest.approx(2.98, abs=0.01)
    gappy = _bars([1.0] * 10)
    assert history_of(gappy[:3] + gappy[9:]).largest_gap_days == 7
    assert history_of([]).limited


def test_todays_bar_is_ignored_until_its_session_has_closed():
    from datetime import datetime, timezone

    from market.markets import NSE, US, completed_bars
    bars = _bars([1.0, 2.0, 3.0])
    today = bars[-1].day
    during = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    after = datetime(today.year, today.month, today.day, 11, 0, tzinfo=timezone.utc)
    assert len(completed_bars(bars, NSE, during)) == 2      # 14:30 IST: still trading
    assert len(completed_bars(bars, NSE, after)) == 3       # 16:30 IST: closed
    assert len(completed_bars(bars, US, after)) == 2        # 06:00 in New York
    tomorrow = datetime(today.year, today.month, today.day + 1, 1, 0, tzinfo=timezone.utc)
    assert len(completed_bars(bars, US, tomorrow)) == 3


def test_the_watchlist_returns_the_index_strip_and_a_card_per_stock():
    out = asyncio.run(service.watchlist(["SBIN.NS", "DIXON.NS"], data=_Recorder()))
    assert out["index"]["ticker"] == "^NSEI" and "signal" not in out["index"]
    assert [c["ticker"] for c in out["stocks"]] == ["SBIN.NS", "DIXON.NS"]
    c = out["stocks"][0]
    assert c["currency_symbol"] == "₹" and len(c["sparkline"]) == service.SPARKLINE_DAYS
    assert c["change_pct"] == pytest.approx((c["sparkline"][-1] / c["sparkline"][-2] - 1) * 100,
                                           abs=0.01)
    assert c["signal"]["signal"] in ("BUY", "SELL", "HOLD") and " of " in c["signal"]["coverage"]
