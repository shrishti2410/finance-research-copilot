"""One call for a broad question about a company: get_company_snapshot.

"What is your analysis of NVIDIA?" on the live site (2026-11-18) spent all five
steps on one get_financial_fact each and never wrote an answer. A broad question
needs a dozen figures; this returns them in one call:

- financials for the last three fiscal years: revenue, gross profit, operating
  income, net income, diluted EPS, with margins and year-on-year growth worked
  out here in Python (fractions: 0.7107 is 71.07%), so the model quotes them
  and does no arithmetic;
  - the 15 US companies: SEC XBRL facts from their 10-K filings, each year with
    its accession number;
  - anything else (the NSE watchlist): Yahoo Finance's annual statements,
    labelled "Yahoo Finance, not a filing";
- the last year's price performance: total return, volatility, max drawdown;
- the signal engine's current output with its coverage and the backtest's
  verdict (from the daily precompute when it is fresh).

A part that cannot be had (no facts, Yahoo down) is reported as missing with the
reason, never guessed; the rest still comes back.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta
from typing import Any

from tools.base import ERROR_BAD_INPUT, error, ok

YEARS = 3
SNAPSHOT_FACTS = ("revenue", "gross_profit", "operating_income", "net_income", "eps_diluted")
YAHOO_ROWS = {
    "revenue": ("Total Revenue", "Operating Revenue", "Revenue"),
    "gross_profit": ("Gross Profit",),
    "operating_income": ("Operating Income", "Total Operating Income As Reported"),
    "net_income": ("Net Income", "Net Income Common Stockholders"),
    "eps_diluted": ("Diluted EPS",),
}
YAHOO_LABEL = "Yahoo Finance annual statements, not a filing"
# Yahoo's statements change at most a few times a year, and one fetch takes
# about 20 s: keep each for half a day.
_YAHOO_TTL_S = 12 * 3600
_yahoo_cache: dict[str, tuple[float, dict]] = {}


def _ratios(years: list[dict]) -> list[dict]:
    """Margins as fractions, and growth against the year before, in place."""
    for i, y in enumerate(years):
        rev = y.get("revenue")
        for name, part in (("gross_margin", "gross_profit"), ("operating_margin", "operating_income"),
                           ("net_margin", "net_income")):
            if rev and y.get(part) is not None:
                y[name] = round(y[part] / rev, 6)
        if i:
            prev = years[i - 1]
            for name, part in (("revenue_growth", "revenue"), ("net_income_growth", "net_income")):
                if prev.get(part) and y.get(part) is not None and prev[part] > 0:
                    y[name] = round(y[part] / prev[part] - 1, 6)
    return years


async def _sec_financials(symbol: str) -> dict | None:
    """The last YEARS fiscal years from SEC XBRL facts, or None if not ingested."""
    from db.base import engine
    from tools.facts import FACTS, _lookup, _number, _years

    async with engine.connect() as conn:
        available = await _years(conn, symbol, FACTS["revenue"][1])
        if not available:
            return None
        rows = []
        for year in available[-(YEARS + 1):]:          # one extra, for the first year's growth
            row: dict[str, Any] = {"fiscal_year": year}
            for fact in SNAPSHOT_FACTS:
                found = await _lookup(conn, symbol, FACTS[fact][1], year)
                if found is not None:
                    row[fact] = _number(found.value)
                    row.setdefault("period_end", found.period_end.isoformat())
                    row.setdefault("accession", found.accession)
            rows.append(row)
    rows = _ratios(rows)[-YEARS:]
    return {"source": "SEC 10-K filings (XBRL)", "currency": "USD", "years": rows}


def _yahoo_financials(symbol: str) -> dict:
    """The last YEARS fiscal years from Yahoo's annual statements."""
    import yfinance as yf

    cached = _yahoo_cache.get(symbol)
    if cached and time.time() - cached[0] < _YAHOO_TTL_S:
        return cached[1]
    handle = yf.Ticker(symbol)
    frame = handle.income_stmt
    if frame is None or getattr(frame, "empty", True):
        raise LookupError(f"Yahoo Finance has no annual statements for {symbol}.")
    rows = []
    for col in list(frame.columns)[: YEARS + 1][::-1]:         # oldest first
        row: dict[str, Any] = {"period_end": col.date().isoformat() if hasattr(col, "date") else str(col)}
        for fact, labels in YAHOO_ROWS.items():
            for label in labels:
                if label in frame.index:
                    value = frame.loc[label, col]
                    if value is not None and value == value:
                        row[fact] = round(float(value), 4) if fact == "eps_diluted" else int(float(value))
                        break
        rows.append(row)
    try:
        currency = handle.fast_info.get("currency")
    except Exception:  # noqa: BLE001
        currency = None
    out = {"source": YAHOO_LABEL, "currency": currency, "years": _ratios(rows)[-YEARS:]}
    _yahoo_cache[symbol] = (time.time(), out)
    return out


async def _financials(symbol: str) -> dict:
    sec = await _sec_financials(symbol)
    if sec is not None:
        return sec
    return await asyncio.to_thread(_yahoo_financials, symbol)


async def _performance(symbol: str) -> dict:
    from market.service import price_analytics

    end = date.today()
    out = await price_analytics(symbol, end - timedelta(days=365), end)
    keep = ("start", "end", "trading_days", "total_return", "annualised_volatility", "max_drawdown",
            "currency")
    return {k: out.get(k) for k in keep}


async def _signal(symbol: str) -> dict:
    from market.service import signal_report

    report = await signal_report(symbol)
    ev = report.get("evidence") or {}
    verdict = report.get("verdict") or {}
    return {
        "signal": ev.get("signal"), "as_of": ev.get("as_of"),
        "coverage": (ev.get("coverage") or {}).get("text"),
        "backtest_verdict": verdict.get("verdict"), "backtest_statement": verdict.get("statement"),
        "note": "A mechanical vote of textbook rules on past prices; not a forecast or a recommendation.",
    }


async def get_company_snapshot(ticker: str) -> dict[str, Any]:
    """Get a company's overview in one call, for broad questions ("what is your
    analysis of X", "how is X doing", comparisons): its last three fiscal years
    of revenue, gross profit, operating income, net income and diluted EPS with
    margins and growth already computed, its last year of stock performance,
    and the signal engine's current output with the backtest verdict.

    US companies with 10-K filings get SEC figures, each year citing its
    filing; others (NSE stocks take ".NS", e.g. "DIXON.NS") get Yahoo Finance
    figures, labelled as not from a filing. Margins and growth are fractions
    (0.7107 means 71.07%). Call it once per company, alongside any other tools
    you need, in your first step.

    Args:
        ticker: Stock ticker, e.g. "NVDA" or "DIXON.NS".

    Returns:
        A dict with "ok": True, "ticker", and "financials" (source, currency,
        years: fiscal_year or period_end, revenue, gross_profit,
        operating_income, net_income, eps_diluted, gross_margin,
        operating_margin, net_margin, revenue_growth, net_income_growth),
        "performance" (1-year total_return, annualised_volatility,
        max_drawdown) and "signal" (signal, coverage, backtest_verdict,
        backtest_statement). A part that could not be had is replaced by
        {"missing": reason}.
    """
    from market.markets import normalize_ticker

    if not ticker or not str(ticker).strip():
        return error(ERROR_BAD_INPUT, "ticker is required, e.g. \"NVDA\" or \"DIXON.NS\".")
    symbol = normalize_ticker(str(ticker))

    async def part(coro):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001  (one part failing must not lose the others)
            return {"missing": f"{type(exc).__name__}: {str(exc)[:160]}"}

    financials, performance, signal = await asyncio.gather(
        part(_financials(symbol)), part(_performance(symbol)), part(_signal(symbol)))
    return ok(ticker=symbol, financials=financials, performance=performance, signal=signal)
