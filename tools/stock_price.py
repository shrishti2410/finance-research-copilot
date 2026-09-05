"""Historical daily stock prices, from Yahoo Finance via yfinance.

Two behaviours here exist because of how a *model* will call this, not because
of how yfinance works:

**`end_date` is inclusive.** yfinance's `end` is exclusive, so asking it for
2026-09-01 to 2026-09-01 returns an empty frame. A model asked "what did NVDA
close at on September 1st" will pass the same date twice, every time. This
function adds the day internally.

**An unknown ticker does not raise.** yfinance returns an empty DataFrame for
`ZZZZNOTREAL` exactly as it does for a range that lands entirely on a weekend.
Both come back as a structured failure, and the two are told apart where they
can be -- see `_no_data_reason`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import yfinance as yf

from tools.base import ERROR_BAD_INPUT, ERROR_NO_DATA, ERROR_UPSTREAM, error, ok

# A model does not need 2,000 daily bars to answer a question, and putting them
# in the transcript costs context that the answer does not. Summary statistics
# are computed over the *whole* requested range regardless of this cap, so
# truncating the series never changes the answer to "how did it do".
MAX_SERIES_POINTS = 250


def _parse_date(value: str, field: str) -> tuple[date | None, dict | None]:
    try:
        return date.fromisoformat(value), None
    except (ValueError, TypeError):
        return None, error(
            ERROR_BAD_INPUT,
            f"{field} must be an ISO date like 2026-01-31, got {value!r}.",
        )


def _no_data_reason(ticker: str, start: date, end: date) -> str:
    """Say which of the two empty-frame causes this was, when that is knowable."""
    weekdays = [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5
    ]
    if not weekdays:
        return (
            f"The range {start} to {end} contains no weekdays, so the market was "
            f"never open. Ask for a range that includes a business day."
        )
    return (
        f"No price data for {ticker!r} between {start} and {end}. Either the "
        f"ticker symbol is wrong, or the market was closed for that whole range "
        f"(a holiday, or a period before the company listed). Check the symbol "
        f"first -- Yahoo Finance returns nothing rather than an error for an "
        f"unknown ticker."
    )


def get_stock_price(ticker: str, start_date: str, end_date: str) -> dict[str, Any]:
    """Get daily historical stock prices for a ticker over a date range.

    Use this for questions about what a stock's price did: closing prices,
    highs and lows, or performance over a period. Prices are split- and
    dividend-adjusted. Data is end-of-day only, delayed, and not suitable for
    intraday or real-time questions.

    Args:
        ticker: Stock ticker symbol, e.g. "NVDA" or "AAPL". Case-insensitive.
            Use the exchange's symbol, not the company name.
        start_date: First date to include, as ISO "YYYY-MM-DD".
        end_date: Last date to include, as ISO "YYYY-MM-DD". Inclusive. To get
            a single day's price, pass the same date for both.

    Returns:
        On success, a dict with "ok": True and:
            ticker: the symbol, upper-cased.
            currency: ISO currency code the prices are quoted in, e.g. "USD".
            trading_days: number of days the market was actually open.
            first, last, high, low: each {"date", "close"} -- the opening and
                closing points of the range, and its extremes by closing price.
            change, change_pct: absolute and percent move from first to last
                close. Percent is a percentage, so 5.2 means +5.2%.
            prices: list of daily bars, oldest first, each with "date", "open",
                "high", "low", "close", "volume".
            series_truncated: True if `prices` holds only the most recent
                portion of the range. The summary fields above always cover the
                full range regardless.

        On failure, {"ok": False, "error": code, "message": ...} where code is
        "bad_input" (malformed dates or reversed range), "no_data" (unknown
        ticker, or the market was closed for the whole range), or
        "upstream_error" (Yahoo Finance unreachable).
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return error(ERROR_BAD_INPUT, "ticker must be a non-empty symbol like 'NVDA'.")

    start, bad = _parse_date(start_date, "start_date")
    if bad:
        return {**bad, "ticker": symbol}
    end, bad = _parse_date(end_date, "end_date")
    if bad:
        return {**bad, "ticker": symbol}

    if start > end:
        return error(
            ERROR_BAD_INPUT,
            f"start_date ({start}) is after end_date ({end}). Swap them.",
            ticker=symbol,
        )

    try:
        # yfinance's `end` is exclusive; ours is inclusive. See module docstring.
        frame = yf.Ticker(symbol).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=True,
        )
    except Exception as exc:  # noqa: BLE001 - network, parsing, upstream schema drift
        return error(
            ERROR_UPSTREAM,
            f"Could not reach Yahoo Finance for {symbol!r}: {type(exc).__name__}: {exc}",
            ticker=symbol,
        )

    if frame is None or frame.empty:
        return error(
            ERROR_NO_DATA, _no_data_reason(symbol, start, end),
            ticker=symbol, start_date=start.isoformat(), end_date=end.isoformat(),
        )

    rows = [
        {
            # The index is tz-aware in the exchange's timezone; .date() drops the
            # time without shifting the calendar day, which .astimezone() would.
            "date": stamp.date().isoformat(),
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
            "volume": int(row["Volume"]),
        }
        for stamp, row in frame.iterrows()
    ]

    first, last = rows[0], rows[-1]
    highest = max(rows, key=lambda r: r["close"])
    lowest = min(rows, key=lambda r: r["close"])
    change = last["close"] - first["close"]

    currency = "USD"
    try:
        currency = yf.Ticker(symbol).fast_info.get("currency") or "USD"
    except Exception:  # noqa: BLE001 - a missing currency is not worth failing the call
        pass

    truncated = len(rows) > MAX_SERIES_POINTS
    return ok(
        ticker=symbol,
        currency=currency,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        trading_days=len(rows),
        first={"date": first["date"], "close": first["close"]},
        last={"date": last["date"], "close": last["close"]},
        high={"date": highest["date"], "close": highest["close"]},
        low={"date": lowest["date"], "close": lowest["close"]},
        change=round(change, 4),
        change_pct=round(change / first["close"] * 100, 4) if first["close"] else None,
        prices=rows[-MAX_SERIES_POINTS:],
        series_truncated=truncated,
        **(
            {"series_note": f"Showing the most recent {MAX_SERIES_POINTS} of "
                            f"{len(rows)} trading days. Summary fields cover all of them."}
            if truncated else {}
        ),
    )
