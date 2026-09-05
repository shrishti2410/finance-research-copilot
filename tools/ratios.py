"""Financial ratios, computed from Yahoo Finance fundamentals via yfinance.

Every result carries the formula and the raw inputs it was computed from, not
just a number. A ratio without its inputs cannot be checked, and a model that
cannot check a number will repeat a wrong one confidently.

Where the numbers come from
---------------------------
`gross_margin`, `operating_margin`, `net_margin`, `return_on_equity`,
`debt_to_equity` and `current_ratio` are computed here from yfinance's
normalized income statement and balance sheet. `pe_ratio` cannot be: it needs a
live market price, which is not in any filing, so it comes from Yahoo's own
trailing calculation.

Two traps in that data, both handled below:

  * `info["debtToEquity"]` is a **percentage** -- Yahoo reports 16.971 for a
    0.17x ratio. Using it raw overstates leverage by 100x. This module computes
    debt-to-equity from the balance sheet instead and never reads that field.
  * yfinance's `Total Debt` on the balance sheet and `info["totalDebt"]` are
    different definitions and differ by ~3.5x for NVDA. The balance-sheet
    figure is the one tied to a stated period end, so that is what is used, and
    the period end is returned alongside the value.


# ---------------------------------------------------------------------------
# WHICH RATIOS STILL NEED OUR INGESTED FILING DATA (Milestone 4), NOT yfinance
# ---------------------------------------------------------------------------
# The ratios above work from yfinance because they are built from top-level
# statement lines that Yahoo normalizes across every company. The following
# cannot be, and are deliberately NOT implemented here -- they need the parsed
# 10-K in `ingestion/`, because the numbers live in places Yahoo does not model:
#
#   * Segment margins (e.g. NVDA Compute & Networking vs Graphics operating
#     margin). Segment tables are a footnote. yfinance has no segment axis at
#     all -- our parser reads them out of the filing's own tables.
#
#   * Anything from a footnote: operating lease obligations, remaining
#     performance obligations, deferred revenue detail, stock-based
#     compensation by function, inventory provisions. NVDA's $7.2B FY2026
#     inventory provision is in MD&A prose and a note; it is in no yfinance
#     field.
#
#   * Non-GAAP measures and their reconciliations. Companies define these
#     themselves in MD&A; there is no normalized source for them by definition.
#
#   * Ratios for a fiscal period older than Yahoo's window. yfinance carries
#     roughly 4-5 annual periods and ~5 quarters. A 10-year trend needs the
#     filings.
#
#   * Anything where the company's own line-item naming matters. Yahoo maps
#     AAPL's "Total net sales" and NVDA's "Revenue" both onto "Total Revenue",
#     which is convenient until the question is specifically about how the
#     company reports it.
#
# The blocker on routing any of these through our own data is coverage, not
# capability: `filing_chunks` currently holds exactly two documents, NVDA FY2026
# and AAPL FY2025. A ratio tool that silently works for two tickers and fails
# for the rest is worse than one that is honestly yfinance-only, so filing-backed
# ratios wait until ingestion covers a real universe.
# ---------------------------------------------------------------------------
"""

from __future__ import annotations

from typing import Any, Literal

import yfinance as yf

from tools.base import (
    ERROR_BAD_INPUT,
    ERROR_NO_DATA,
    ERROR_UNSUPPORTED,
    ERROR_UPSTREAM,
    error,
    ok,
)

RatioName = Literal[
    "gross_margin",
    "operating_margin",
    "net_margin",
    "return_on_equity",
    "debt_to_equity",
    "current_ratio",
    "pe_ratio",
]

Period = Literal["annual", "quarterly", "ttm"]

SUPPORTED_RATIOS: tuple[str, ...] = (
    "gross_margin", "operating_margin", "net_margin", "return_on_equity",
    "debt_to_equity", "current_ratio", "pe_ratio",
)
SUPPORTED_PERIODS: tuple[str, ...] = ("annual", "quarterly", "ttm")

# Ratios expressed as a percentage when rendered, rather than a multiple.
_PERCENT_RATIOS = {"gross_margin", "operating_margin", "net_margin", "return_on_equity"}

# yfinance's row labels drift between versions and across companies, so every
# lookup tries a list of candidates rather than one exact string.
_ROW_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("Total Revenue", "Operating Revenue", "Revenue"),
    "gross_profit": ("Gross Profit",),
    "cost_of_revenue": ("Cost Of Revenue", "Reconciled Cost Of Revenue"),
    "operating_income": ("Operating Income", "Total Operating Income As Reported"),
    "net_income": (
        "Net Income",
        "Net Income Common Stockholders",
        "Net Income From Continuing Operation Net Minority Interest",
    ),
    "total_debt": ("Total Debt",),
    "equity": ("Stockholders Equity", "Total Equity Gross Minority Interest"),
    "current_assets": ("Current Assets", "Total Current Assets"),
    "current_liabilities": ("Current Liabilities", "Total Current Liabilities"),
}


def _pick_row(frame, key: str) -> float | None:
    """First matching row's most recent value, or None if no alias is present."""
    if frame is None or getattr(frame, "empty", True):
        return None
    for label in _ROW_ALIASES[key]:
        if label in frame.index:
            value = frame.loc[label].iloc[0]
            if value is not None and value == value:  # NaN != NaN
                return float(value)
    return None


def _period_end(frame) -> str | None:
    if frame is None or getattr(frame, "empty", True) or not len(frame.columns):
        return None
    stamp = frame.columns[0]
    return stamp.date().isoformat() if hasattr(stamp, "date") else str(stamp)


def _statements(handle, period: str):
    """(income statement, balance sheet) for the requested period.

    A balance sheet has no trailing-twelve-month form -- it is a snapshot, not a
    flow -- so "ttm" pairs the TTM income statement with the most recent
    quarterly balance sheet, which is what a TTM return-on-equity means anyway.
    """
    if period == "quarterly":
        return handle.quarterly_income_stmt, handle.quarterly_balance_sheet
    if period == "ttm":
        return handle.ttm_income_stmt, handle.quarterly_balance_sheet
    return handle.income_stmt, handle.balance_sheet


def calculate_ratio(
    ticker: str,
    ratio_name: RatioName,
    period: Period = "annual",
) -> dict[str, Any]:
    """Calculate a financial ratio for a company from its reported financials.

    Use this for questions about profitability, leverage, liquidity or
    valuation. The result includes the formula used and the raw dollar figures
    it was computed from, so the number can be checked rather than trusted.

    Args:
        ticker: Stock ticker symbol, e.g. "NVDA" or "AAPL". Case-insensitive.
        ratio_name: Which ratio to compute. One of:
            "gross_margin" - gross profit / revenue. How much of each sales
                dollar survives the direct cost of producing the goods.
            "operating_margin" - operating income / revenue.
            "net_margin" - net income / revenue.
            "return_on_equity" - net income / shareholders' equity.
            "debt_to_equity" - total debt / shareholders' equity. A leverage
                measure; returned as a multiple, so 0.07 means debt is 7% of
                equity.
            "current_ratio" - current assets / current liabilities. Short-term
                liquidity; below 1.0 means current liabilities exceed current
                assets.
            "pe_ratio" - share price / trailing twelve-month earnings per
                share. Always as-of-now, because it depends on the live market
                price; the `period` argument does not apply to it.
        period: Which reporting period to use. One of:
            "annual" - the most recent completed fiscal year (default).
            "quarterly" - the most recent reported quarter.
            "ttm" - trailing twelve months.
            Fiscal years are the company's own, not calendar: NVDA's FY2026
            ended January 2026. The period end date is always returned.

    Returns:
        On success, a dict with "ok": True and:
            ticker, ratio, period: what was asked for, normalized.
            period_end: ISO date the figures are as of, e.g. "2026-01-31".
            value: the ratio as a float. Margins and return_on_equity are
                fractions, so 0.7107 means 71.07%. debt_to_equity,
                current_ratio and pe_ratio are multiples.
            formatted: the same number rendered for a human, e.g. "71.07%" or
                "0.07x".
            formula: the arithmetic used, e.g. "gross_profit / total_revenue".
            inputs: the raw figures, in `currency` units, that went into it.
            source: which upstream dataset the inputs came from.

        On failure, {"ok": False, "error": code, "message": ...} where code is
        "bad_input" (empty ticker), "unsupported" (unknown ratio or period --
        the message lists the valid values), "no_data" (unknown ticker, or the
        company does not report the needed line items, e.g. a bank has no
        current assets and a loss-making company has no meaningful P/E), or
        "upstream_error" (Yahoo Finance unreachable).
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return error(ERROR_BAD_INPUT, "ticker must be a non-empty symbol like 'NVDA'.")

    ratio = (ratio_name or "").strip().lower()
    if ratio not in SUPPORTED_RATIOS:
        return error(
            ERROR_UNSUPPORTED,
            f"Unknown ratio {ratio_name!r}. Supported: {', '.join(SUPPORTED_RATIOS)}.",
            ticker=symbol,
        )

    requested_period = (period or "annual").strip().lower()
    if requested_period not in SUPPORTED_PERIODS:
        return error(
            ERROR_UNSUPPORTED,
            f"Unknown period {period!r}. Supported: {', '.join(SUPPORTED_PERIODS)}.",
            ticker=symbol,
        )

    try:
        handle = yf.Ticker(symbol)
        if ratio == "pe_ratio":
            return _pe_ratio(symbol, handle, requested_period)
        income, balance = _statements(handle, requested_period)
    except Exception as exc:  # noqa: BLE001 - network, upstream schema drift
        return error(
            ERROR_UPSTREAM,
            f"Could not reach Yahoo Finance for {symbol!r}: {type(exc).__name__}: {exc}",
            ticker=symbol,
        )

    figures = {
        "revenue": _pick_row(income, "revenue"),
        "gross_profit": _pick_row(income, "gross_profit"),
        "operating_income": _pick_row(income, "operating_income"),
        "net_income": _pick_row(income, "net_income"),
        "total_debt": _pick_row(balance, "total_debt"),
        "equity": _pick_row(balance, "equity"),
        "current_assets": _pick_row(balance, "current_assets"),
        "current_liabilities": _pick_row(balance, "current_liabilities"),
    }

    if all(value is None for value in figures.values()):
        return error(
            ERROR_NO_DATA,
            f"No {requested_period} financial statements for {symbol!r}. Either the "
            f"ticker symbol is wrong, or it is an instrument that does not file "
            f"financials, such as an ETF or an index.",
            ticker=symbol, ratio=ratio, period=requested_period,
        )

    recipes: dict[str, tuple[str, str, str]] = {
        "gross_margin": ("gross_profit", "revenue", "gross_profit / revenue"),
        "operating_margin": ("operating_income", "revenue", "operating_income / revenue"),
        "net_margin": ("net_income", "revenue", "net_income / revenue"),
        "return_on_equity": ("net_income", "equity", "net_income / shareholders_equity"),
        "debt_to_equity": ("total_debt", "equity", "total_debt / shareholders_equity"),
        "current_ratio": (
            "current_assets", "current_liabilities",
            "current_assets / current_liabilities",
        ),
    }
    numerator_key, denominator_key, formula = recipes[ratio]
    numerator, denominator = figures[numerator_key], figures[denominator_key]

    missing = [
        name for name, value in ((numerator_key, numerator), (denominator_key, denominator))
        if value is None
    ]
    if missing:
        return error(
            ERROR_NO_DATA,
            f"{symbol} does not report {' and '.join(missing)} in its "
            f"{requested_period} statements, so {ratio} cannot be computed. This is "
            f"normal for some business models -- banks and insurers do not present a "
            f"classified balance sheet, so they have no current assets or liabilities.",
            ticker=symbol, ratio=ratio, period=requested_period,
        )

    if denominator == 0:
        return error(
            ERROR_NO_DATA,
            f"{symbol}'s {denominator_key} is zero for the {requested_period} period, "
            f"so {ratio} is undefined.",
            ticker=symbol, ratio=ratio, period=requested_period,
        )

    value = numerator / denominator
    # Negative equity makes debt_to_equity and ROE arithmetically fine and
    # financially meaningless -- a negative leverage ratio reads as "low
    # leverage" to anything that does not know better.
    caveat = None
    if denominator_key == "equity" and denominator < 0:
        caveat = (
            f"{symbol} has negative shareholders' equity "
            f"({denominator:,.0f}), which makes this ratio negative and not "
            f"comparable to a normal one. Read it as 'equity is depleted', not "
            f"as low leverage."
        )

    source_frame = income if numerator_key in (
        "revenue", "gross_profit", "operating_income", "net_income"
    ) else balance
    inputs = {numerator_key: numerator, denominator_key: denominator}

    return ok(
        ticker=symbol,
        ratio=ratio,
        period=requested_period,
        period_end=_period_end(source_frame),
        balance_sheet_date=_period_end(balance) if "equity" in inputs
        or denominator_key.startswith("current") else None,
        value=round(value, 6),
        formatted=(f"{value * 100:.2f}%" if ratio in _PERCENT_RATIOS else f"{value:.2f}x"),
        formula=formula,
        inputs={key: round(val, 2) for key, val in inputs.items()},
        currency=_currency(handle),
        source=f"yfinance {requested_period} income_stmt / balance_sheet",
        **({"caveat": caveat} if caveat else {}),
    )


def _pe_ratio(symbol: str, handle, requested_period: str) -> dict[str, Any]:
    """P/E from Yahoo's own trailing calculation.

    Not computable from the statements alone: the numerator is a live market
    price, which appears in no filing. Kept in this tool anyway because a model
    asking for "the P/E" should not have to know that.
    """
    try:
        info = handle.info or {}
    except Exception as exc:  # noqa: BLE001
        return error(
            ERROR_UPSTREAM,
            f"Could not reach Yahoo Finance for {symbol!r}: {type(exc).__name__}: {exc}",
            ticker=symbol,
        )

    trailing = info.get("trailingPE")
    if trailing is None:
        forward = info.get("forwardPE")
        detail = (
            f" Yahoo does report a forward P/E of {forward:.2f}, based on estimated "
            f"future earnings rather than reported ones."
            if isinstance(forward, (int, float)) else ""
        )
        return error(
            ERROR_NO_DATA,
            f"No trailing P/E for {symbol!r}. This is expected when the company's "
            f"trailing twelve-month earnings are negative or zero -- a P/E is "
            f"undefined for a loss-making company.{detail}",
            ticker=symbol, ratio="pe_ratio", period="ttm",
        )

    result = ok(
        ticker=symbol,
        ratio="pe_ratio",
        period="ttm",
        period_end=None,
        value=round(float(trailing), 6),
        formatted=f"{float(trailing):.2f}x",
        formula="share_price / trailing_twelve_month_eps",
        inputs={
            "trailing_eps": info.get("trailingEps"),
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        },
        currency=info.get("currency", "USD"),
        source="yfinance info['trailingPE']",
    )
    if requested_period != "ttm":
        result["period_note"] = (
            f"P/E depends on the current share price, so it is only meaningful as of "
            f"now. The requested period {requested_period!r} does not apply and "
            f"trailing twelve months was used instead."
        )
    return result


def _currency(handle) -> str:
    try:
        return handle.fast_info.get("currency") or "USD"
    except Exception:  # noqa: BLE001 - a missing currency is not worth failing the call
        return "USD"
