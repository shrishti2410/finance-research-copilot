"""Tests for the ratio tool. No live calls -- `yf` is replaced wholesale.

The fixture figures are NVDA's real FY2026 and FY2025 numbers, so the expected
ratios are checkable against the filing: revenue 215,938M, cost of revenue
62,475M, gross profit 153,463M.
"""

import pandas as pd
import pytest

from tools import ratios
from tools.base import ERROR_BAD_INPUT, ERROR_NO_DATA, ERROR_UNSUPPORTED, ERROR_UPSTREAM

MILLION = 1_000_000

ANNUAL_INCOME = pd.DataFrame(
    {
        pd.Timestamp("2026-01-31"): [215_938, 62_475, 153_463, 139_297, 120_067],
        pd.Timestamp("2025-01-31"): [130_497, 32_639, 97_858, 81_453, 72_880],
    },
    index=["Total Revenue", "Cost Of Revenue", "Gross Profit",
           "Operating Income", "Net Income"],
) * MILLION

ANNUAL_BALANCE = pd.DataFrame(
    {
        pd.Timestamp("2026-01-31"): [11_040, 157_293, 180_000, 60_000],
        pd.Timestamp("2025-01-31"): [9_982, 79_327, 120_000, 45_000],
    },
    index=["Total Debt", "Stockholders Equity", "Current Assets",
           "Current Liabilities"],
) * MILLION

QUARTERLY_INCOME = pd.DataFrame(
    {pd.Timestamp("2026-04-30"): [62_000, 18_000, 44_000, 40_000, 35_000]},
    index=["Total Revenue", "Cost Of Revenue", "Gross Profit",
           "Operating Income", "Net Income"],
) * MILLION

INFO = {
    "trailingPE": 29.159492,
    "forwardPE": 14.902219,
    "trailingEps": 6.87,
    "currentPrice": 200.35,
    "currency": "USD",
    # Yahoo reports this as a percentage. Reading it raw would report NVDA as
    # 17x levered instead of 0.17x -- this module must never touch it.
    "debtToEquity": 16.971,
}


class FakeTicker:
    def __init__(self, state):
        self._state = state

    @property
    def income_stmt(self):
        return self._state["annual_income"]

    @property
    def balance_sheet(self):
        return self._state["annual_balance"]

    @property
    def quarterly_income_stmt(self):
        return self._state["quarterly_income"]

    @property
    def quarterly_balance_sheet(self):
        return self._state["annual_balance"]

    @property
    def ttm_income_stmt(self):
        return self._state["quarterly_income"]

    @property
    def info(self):
        if self._state["raises"]:
            raise self._state["raises"]
        return self._state["info"]

    @property
    def fast_info(self):
        return {"currency": "USD"}


@pytest.fixture
def yf(monkeypatch):
    state = {
        "annual_income": ANNUAL_INCOME,
        "annual_balance": ANNUAL_BALANCE,
        "quarterly_income": QUARTERLY_INCOME,
        "info": INFO,
        "raises": None,
    }

    class FakeYF:
        @staticmethod
        def Ticker(symbol):
            return FakeTicker(state)

    monkeypatch.setattr(ratios, "yf", FakeYF)
    return state


EMPTY = pd.DataFrame()


# ── the three required ratios ────────────────────────────────────────────────

def test_gross_margin(yf):
    result = ratios.calculate_ratio("NVDA", "gross_margin", "annual")

    assert result["ok"] is True
    assert result["value"] == pytest.approx(153_463 / 215_938, abs=1e-6)
    assert result["formatted"] == "71.07%"
    assert result["formula"] == "gross_profit / revenue"
    assert result["inputs"] == {"gross_profit": 153_463 * MILLION, "revenue": 215_938 * MILLION}
    assert result["period_end"] == "2026-01-31"


def test_pe_ratio_comes_from_the_market_not_the_statements(yf):
    """A P/E needs a live share price, which appears in no filing."""
    result = ratios.calculate_ratio("NVDA", "pe_ratio")

    assert result["ok"] is True
    assert result["value"] == pytest.approx(29.159492)
    assert result["formatted"] == "29.16x"
    assert result["period"] == "ttm"
    assert result["inputs"]["price"] == 200.35


def test_debt_to_equity_is_computed_not_taken_from_info(yf):
    """info['debtToEquity'] is a percentage: 16.971 means 0.17x. Returning it
    raw would overstate leverage by 100x."""
    result = ratios.calculate_ratio("NVDA", "debt_to_equity", "annual")

    assert result["value"] == pytest.approx(11_040 / 157_293, abs=1e-6)
    assert result["value"] < 0.1
    assert result["value"] != pytest.approx(INFO["debtToEquity"])
    assert result["formatted"] == "0.07x"
    assert result["formula"] == "total_debt / shareholders_equity"


# ── the rest of the set ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "ratio,expected",
    [
        ("operating_margin", 139_297 / 215_938),
        ("net_margin", 120_067 / 215_938),
        ("return_on_equity", 120_067 / 157_293),
        ("current_ratio", 180_000 / 60_000),
    ],
)
def test_other_supported_ratios(yf, ratio, expected):
    assert ratios.calculate_ratio("NVDA", ratio)["value"] == pytest.approx(expected, abs=1e-6)


def test_margins_render_as_percentages_and_multiples_as_x(yf):
    assert ratios.calculate_ratio("NVDA", "net_margin")["formatted"].endswith("%")
    assert ratios.calculate_ratio("NVDA", "current_ratio")["formatted"].endswith("x")


def test_every_advertised_ratio_is_actually_implemented(yf):
    """The docstring is the model's schema; a listed ratio that returns
    'unsupported' is a broken contract."""
    for name in ratios.SUPPORTED_RATIOS:
        assert ratios.calculate_ratio("NVDA", name)["ok"] is True, name


# ── periods ──────────────────────────────────────────────────────────────────

def test_quarterly_period_uses_the_quarterly_statements(yf):
    result = ratios.calculate_ratio("NVDA", "gross_margin", "quarterly")
    assert result["period_end"] == "2026-04-30"
    assert result["value"] == pytest.approx(44_000 / 62_000, abs=1e-6)


def test_ttm_period_uses_the_ttm_income_statement(yf):
    assert ratios.calculate_ratio("NVDA", "net_margin", "ttm")["value"] == pytest.approx(
        35_000 / 62_000, abs=1e-6
    )


def test_pe_explains_that_period_does_not_apply(yf):
    """Silently ignoring the argument would let a model believe it got an
    annual P/E."""
    result = ratios.calculate_ratio("NVDA", "pe_ratio", "annual")
    assert result["period"] == "ttm"
    assert "does not apply" in result["period_note"]


def test_ttm_is_not_flagged_with_a_period_note(yf):
    assert "period_note" not in ratios.calculate_ratio("NVDA", "pe_ratio", "ttm")


# ── row-label resilience ─────────────────────────────────────────────────────

def test_alternative_row_labels_are_recognised(yf):
    """yfinance's labels differ across companies and versions; one exact string
    would make this tool fail on the next name it meets."""
    yf["annual_balance"] = ANNUAL_BALANCE.rename(
        index={"Current Assets": "Total Current Assets",
               "Current Liabilities": "Total Current Liabilities"}
    )
    assert ratios.calculate_ratio("NVDA", "current_ratio")["value"] == pytest.approx(3.0)


def test_nan_values_are_treated_as_missing(yf):
    yf["annual_income"] = ANNUAL_INCOME.copy()
    yf["annual_income"].loc["Gross Profit"] = float("nan")
    assert ratios.calculate_ratio("NVDA", "gross_margin")["error"] == ERROR_NO_DATA


# ── bad and unsupported input ────────────────────────────────────────────────

def test_unknown_ratio_lists_the_supported_ones(yf):
    result = ratios.calculate_ratio("NVDA", "ebitda_margin")
    assert result["error"] == ERROR_UNSUPPORTED
    assert "gross_margin" in result["message"]


def test_unknown_period_lists_the_supported_ones(yf):
    result = ratios.calculate_ratio("NVDA", "gross_margin", "monthly")
    assert result["error"] == ERROR_UNSUPPORTED
    assert "annual" in result["message"]


def test_empty_ticker_is_rejected(yf):
    assert ratios.calculate_ratio("", "gross_margin")["error"] == ERROR_BAD_INPUT


def test_ratio_name_is_case_insensitive(yf):
    assert ratios.calculate_ratio("nvda", "Gross_Margin")["ok"] is True


# ── missing data ─────────────────────────────────────────────────────────────

def test_no_statements_at_all_reads_as_a_bad_ticker_or_an_etf(yf):
    yf["annual_income"] = EMPTY
    yf["annual_balance"] = EMPTY
    result = ratios.calculate_ratio("SPY", "gross_margin")

    assert result["error"] == ERROR_NO_DATA
    assert "ETF" in result["message"]


def test_a_missing_line_item_explains_which_one(yf):
    """Banks present no classified balance sheet, so current_ratio is not a
    failure to look up -- it does not exist for them."""
    yf["annual_balance"] = ANNUAL_BALANCE.drop(index=["Current Assets"])
    result = ratios.calculate_ratio("JPM", "current_ratio")

    assert result["error"] == ERROR_NO_DATA
    assert "current_assets" in result["message"]
    assert "banks" in result["message"].lower()


def test_zero_denominator_is_undefined_not_a_crash(yf):
    yf["annual_income"] = ANNUAL_INCOME.copy()
    yf["annual_income"].loc["Total Revenue"] = 0
    result = ratios.calculate_ratio("NVDA", "gross_margin")

    assert result["error"] == ERROR_NO_DATA
    assert "undefined" in result["message"]


def test_missing_trailing_pe_mentions_the_forward_one(yf):
    """A loss-making company has no trailing P/E; that is the answer, and the
    forward figure is the useful thing to offer instead."""
    yf["info"] = {**INFO, "trailingPE": None}
    result = ratios.calculate_ratio("RIVN", "pe_ratio")

    assert result["error"] == ERROR_NO_DATA
    assert "loss-making" in result["message"]
    assert "14.90" in result["message"]


# ── financially meaningless results ──────────────────────────────────────────

def test_negative_equity_is_flagged_not_silently_returned(yf):
    """A negative debt-to-equity reads as 'low leverage' to anything that does
    not know equity is negative."""
    yf["annual_balance"] = ANNUAL_BALANCE.copy()
    yf["annual_balance"].loc["Stockholders Equity"] = -5_000 * MILLION
    result = ratios.calculate_ratio("MCD", "debt_to_equity")

    assert result["ok"] is True
    assert result["value"] < 0
    assert "negative shareholders' equity" in result["caveat"]


# ── upstream failure ─────────────────────────────────────────────────────────

def test_network_failure_becomes_an_error_dict(yf):
    yf["raises"] = ConnectionError("connection reset")
    result = ratios.calculate_ratio("NVDA", "pe_ratio")

    assert result["error"] == ERROR_UPSTREAM
    assert "connection reset" in result["message"]
