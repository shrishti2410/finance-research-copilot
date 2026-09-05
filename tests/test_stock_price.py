"""Tests for the stock price tool. No live calls -- `yf` is replaced wholesale.

The fake returns real pandas DataFrames with a tz-aware DatetimeIndex, because
that is what yfinance returns and the date handling in the tool is the part most
likely to be wrong.
"""

import pandas as pd
import pytest

from tools import stock_price
from tools.base import ERROR_BAD_INPUT, ERROR_NO_DATA, ERROR_UPSTREAM


def frame(rows: list[tuple[str, float, float, float, float, int]]) -> pd.DataFrame:
    index = pd.DatetimeIndex(
        [pd.Timestamp(day, tz="America/New_York") for day, *_ in rows], name="Date"
    )
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Volume": [r[5] for r in rows],
            "Dividends": [0.0] * len(rows),
            "Stock Splits": [0.0] * len(rows),
        },
        index=index,
    )


THREE_DAYS = frame([
    ("2026-08-03", 197.69, 208.74, 197.00, 207.50, 300_000_000),
    ("2026-08-04", 211.30, 213.06, 209.10, 212.00, 250_000_000),
    ("2026-08-05", 216.86, 222.22, 215.00, 220.75, 400_000_000),
])


class FakeTicker:
    """Records what it was asked for, so the tool's date arithmetic is checkable."""

    calls: list[dict] = []

    def __init__(self, symbol, result=THREE_DAYS, currency="USD", raises=None):
        self.symbol = symbol
        self._result = result
        self._currency = currency
        self._raises = raises

    def history(self, **kwargs):
        FakeTicker.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return self._result

    @property
    def fast_info(self):
        return {"currency": self._currency}


@pytest.fixture
def yf(monkeypatch):
    """Install a fake yfinance and hand back a knob to configure it."""
    FakeTicker.calls = []
    state: dict = {"result": THREE_DAYS, "currency": "USD", "raises": None}

    class FakeYF:
        @staticmethod
        def Ticker(symbol):
            return FakeTicker(
                symbol, state["result"], state["currency"], state["raises"]
            )

    monkeypatch.setattr(stock_price, "yf", FakeYF)
    return state


# ── happy path ───────────────────────────────────────────────────────────────

def test_returns_the_daily_series(yf):
    result = stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")

    assert result["ok"] is True
    assert result["ticker"] == "NVDA"
    assert result["currency"] == "USD"
    assert result["trading_days"] == 3
    assert [bar["date"] for bar in result["prices"]] == [
        "2026-08-03", "2026-08-04", "2026-08-05"
    ]
    assert result["prices"][0]["volume"] == 300_000_000


def test_summary_statistics(yf):
    result = stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")

    assert result["first"] == {"date": "2026-08-03", "close": 207.5}
    assert result["last"] == {"date": "2026-08-05", "close": 220.75}
    assert result["high"]["close"] == 220.75
    assert result["low"]["close"] == 207.5
    assert result["change"] == pytest.approx(13.25)
    assert result["change_pct"] == pytest.approx(6.3855, abs=1e-3)


def test_ticker_is_upper_cased(yf):
    assert stock_price.get_stock_price("nvda", "2026-08-03", "2026-08-05")["ticker"] == "NVDA"


def test_dates_are_not_shifted_by_the_exchange_timezone(yf):
    """The index is tz-aware in New York. Converting to UTC before taking the
    date would move a midnight bar onto the following day."""
    result = stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")
    assert result["prices"][0]["date"] == "2026-08-03"


# ── the inclusive-end contract ───────────────────────────────────────────────

def test_end_date_is_inclusive(yf):
    """yfinance's `end` is exclusive, so it must be handed the day after. A model
    asking for one day's close passes the same date twice; without this it gets
    an empty frame."""
    stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")
    assert FakeTicker.calls[0]["end"] == "2026-08-06"
    assert FakeTicker.calls[0]["start"] == "2026-08-03"


def test_single_day_request_is_not_an_empty_range(yf):
    yf["result"] = THREE_DAYS.iloc[:1]
    result = stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-03")
    assert result["ok"] is True and result["trading_days"] == 1
    assert FakeTicker.calls[0]["end"] == "2026-08-04"


def test_prices_are_adjusted(yf):
    """A split makes an unadjusted series discontinuous mid-range."""
    stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")
    assert FakeTicker.calls[0]["auto_adjust"] is True


# ── empty data ───────────────────────────────────────────────────────────────

def test_empty_frame_is_a_structured_error_not_a_crash(yf):
    """yfinance returns an empty DataFrame for an unknown ticker rather than
    raising, which is the whole reason this branch exists."""
    yf["result"] = THREE_DAYS.iloc[0:0]
    result = stock_price.get_stock_price("ZZZZNOTREAL", "2026-08-03", "2026-08-05")

    assert result["ok"] is False
    assert result["error"] == ERROR_NO_DATA
    assert "ticker symbol is wrong" in result["message"]
    assert result["ticker"] == "ZZZZNOTREAL"


def test_a_weekend_only_range_says_so_definitively(yf):
    """Distinguishable from a bad ticker without a second network call: if the
    range holds no weekday, the market cannot have been open."""
    yf["result"] = THREE_DAYS.iloc[0:0]
    result = stock_price.get_stock_price("NVDA", "2026-08-08", "2026-08-09")

    assert result["error"] == ERROR_NO_DATA
    assert "no weekdays" in result["message"]


def test_none_result_is_handled_like_an_empty_frame(yf):
    yf["result"] = None
    assert stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")["error"] == (
        ERROR_NO_DATA
    )


# ── bad input ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["08/03/2026", "2026-13-01", "yesterday", ""])
def test_malformed_dates_are_rejected(yf, bad):
    result = stock_price.get_stock_price("NVDA", bad, "2026-08-05")
    assert result["error"] == ERROR_BAD_INPUT
    assert "ISO date" in result["message"]


def test_reversed_range_is_rejected(yf):
    result = stock_price.get_stock_price("NVDA", "2026-08-05", "2026-08-03")
    assert result["error"] == ERROR_BAD_INPUT
    assert "after end_date" in result["message"]


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_ticker_is_rejected(yf, bad):
    assert stock_price.get_stock_price(bad, "2026-08-03", "2026-08-05")["error"] == (
        ERROR_BAD_INPUT
    )


def test_bad_input_never_reaches_the_network(yf):
    stock_price.get_stock_price("NVDA", "not-a-date", "2026-08-05")
    assert FakeTicker.calls == []


# ── upstream failure ─────────────────────────────────────────────────────────

def test_network_failure_becomes_an_error_dict(yf):
    yf["raises"] = ConnectionError("connection reset")
    result = stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")

    assert result["ok"] is False
    assert result["error"] == ERROR_UPSTREAM
    assert "connection reset" in result["message"]


def test_a_missing_currency_does_not_fail_the_call(monkeypatch):
    """The prices are the answer; the currency label is a nicety."""
    class Broken:
        def history(self, **kwargs):
            return THREE_DAYS

        @property
        def fast_info(self):
            raise RuntimeError("no fast_info")

    monkeypatch.setattr(stock_price, "yf", type("YF", (), {"Ticker": staticmethod(lambda s: Broken())}))
    result = stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")
    assert result["ok"] is True and result["currency"] == "USD"


# ── long ranges ──────────────────────────────────────────────────────────────

def test_long_series_is_truncated_but_summary_covers_everything(yf):
    """A model does not need 400 bars, but 'how did it do over 2 years' must
    still be answered from the whole range, not from the visible tail."""
    days = pd.bdate_range("2024-01-01", periods=400)
    yf["result"] = frame([
        (day.strftime("%Y-%m-%d"), 10.0, 10.0, 10.0, float(100 + i), 1_000)
        for i, day in enumerate(days)
    ])
    result = stock_price.get_stock_price("NVDA", "2024-01-01", "2025-07-01")

    assert result["trading_days"] == 400
    assert len(result["prices"]) == stock_price.MAX_SERIES_POINTS
    assert result["series_truncated"] is True
    assert "series_note" in result
    # The extremes come from outside the returned window.
    assert result["low"]["close"] == 100.0
    assert result["first"]["close"] == 100.0
    assert result["high"]["close"] == 499.0


def test_short_series_is_not_flagged_as_truncated(yf):
    assert stock_price.get_stock_price(
        "NVDA", "2026-08-03", "2026-08-05"
    )["series_truncated"] is False
