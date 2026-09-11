"""Historical daily stock prices, from Yahoo Finance via yfinance.

Two behaviours here exist because of how a *model* will call this, not because
of how yfinance works:

**`end_date` is inclusive, and optional.** yfinance's `end` is exclusive, so
asking it for 2026-09-01 to 2026-09-01 returns an empty frame. A model asked
"what did NVDA close at on September 1st" will pass the same date twice, every
time. This function adds the day internally -- and accepts the call with
end_date left out, because a smaller model asked for a current price omitted it
and then gave up rather than reading the error and retrying.

**An unknown ticker does not raise.** yfinance returns an empty DataFrame for
`ZZZZNOTREAL` exactly as it does for a range that lands entirely on a weekend.
Both come back as a structured failure, and the two are told apart where they
can be -- see `_no_data_reason`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import yfinance as yf

from tools._yahoo import history_timeout
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


def get_stock_price(ticker: str, start_date: str, end_date: str = "") -> dict[str, Any]:
    """Get daily historical stock prices for a ticker over a date range.

    Use this for questions about what a stock's price did: closing prices,
    highs and lows, or performance over a period. Prices are split- and
    dividend-adjusted. Data is end-of-day only, delayed, and not suitable for
    intraday or real-time questions.

    Args:
        ticker: Stock ticker symbol, e.g. "NVDA" or "AAPL". Case-insensitive.
            Use the exchange's symbol, not the company name.
        start_date: First date to include, as ISO "YYYY-MM-DD".
        end_date: Last date to include, as ISO "YYYY-MM-DD". Inclusive.
            Optional: omit it for a single day's price and it defaults to
            start_date.

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
    # Omitting end_date means one day. Asked for a current price, qwen2.5:1.5b
    # called this with start_date alone, read the "requires end_date" envelope,
    # and answered "I couldn't find the current stock price" rather than
    # retrying with the date -- a false negative produced entirely by making a
    # model say the same date twice. A single-day range is what a bare
    # start_date can only mean.
    end, bad = _parse_date(end_date or start_date, "end_date")
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
            # Stated here rather than inherited. yfinance's own default is 10s
            # and could change under an upgrade; retries are configured in
            # tools/_yahoo.py, using the library's built-in backoff.
            timeout=history_timeout(),
        )
    except Exception as exc:  # noqa: BLE001 - network, parsing, upstream schema drift
        return error(
            ERROR_UPSTREAM,
            f"Could not reach Yahoo Finance for {symbol!r}: {type(exc).__name__}: {exc}",
            ticker=symbol,
        )

    # Reaching Yahoo Finance and getting something back are two different
    # things. `frame` here is whatever the library handed over, and the checks
    # below are about its shape rather than the network.
    try:
        if frame is None or frame.empty:
            return error(
                ERROR_NO_DATA, _no_data_reason(symbol, start, end),
                ticker=symbol, start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
    except AttributeError:
        # Not a DataFrame at all. Measured: a stubbed upstream returning a
        # string got as far as `.empty` and raised AttributeError out of the
        # tool, which the registry then relayed to the model as
        # "get_stock_price raised AttributeError: 'str' object has no attribute
        # 'empty'" -- a sentence about our internals, in an answer about a share
        # price.
        return error(
            ERROR_UPSTREAM,
            f"Yahoo Finance returned {type(frame).__name__} instead of a price "
            f"table for {symbol!r}. This is an upstream format problem, not a "
            f"bad request.",
            ticker=symbol,
        )

    missing = [column for column in ("Open", "High", "Low", "Close", "Volume")
               if column not in getattr(frame, "columns", ())]
    if missing:
        # Same class of failure as above and the more likely one: Yahoo changes
        # a column name and every price question starts answering with a
        # KeyError.
        return error(
            ERROR_UPSTREAM,
            f"Yahoo Finance returned a price table for {symbol!r} without "
            f"{', '.join(missing)}. The upstream format has changed; this is "
            f"not a bad request.",
            ticker=symbol,
        )

    try:
        rows = [
            {
                # The index is tz-aware in the exchange's timezone; .date() drops
                # the time without shifting the calendar day, which
                # .astimezone() would.
                "date": stamp.date().isoformat(),
                "open": round(float(row["Open"]), 4),
                "high": round(float(row["High"]), 4),
                "low": round(float(row["Low"]), 4),
                "close": round(float(row["Close"]), 4),
                "volume": int(row["Volume"]),
            }
            for stamp, row in frame.iterrows()
        ]
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        # A row whose values are not numbers, or an index that is not dates.
        return error(
            ERROR_UPSTREAM,
            f"Could not read the price table Yahoo Finance returned for "
            f"{symbol!r}: {type(exc).__name__}: {exc}",
            ticker=symbol,
        )

    if not rows:
        return error(
            ERROR_NO_DATA, _no_data_reason(symbol, start, end),
            ticker=symbol, start_date=start.isoformat(), end_date=end.isoformat(),
        )

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
