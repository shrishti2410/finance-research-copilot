"""Which market a ticker trades on, its currency, and its trading costs.

    market_for("SBIN.NS")   -> NSE, INR, ₹, Indian equity delivery charges
    market_for("^NSEI")     -> NSE (the Nifty 50 index), INR
    market_for("NVDA")      -> US, USD, $, a flat basis-point cost

Yahoo Finance names an NSE stock with a ".NS" suffix and a BSE one with
".BO"; an index starts with "^". A US class share is written "BRK.B" by people
and "BRK-B" by Yahoo, so a one-letter suffix after a dot is a share class and
anything longer is an exchange.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

__all__ = ["Market", "market_for", "normalize_ticker", "completed_bars", "INDIAN_SUFFIXES"]

INDIAN_SUFFIXES = {"NS": "NSE", "BO": "BSE"}
INDIAN_INDICES = {"^NSEI": "NSE", "^NSEBANK": "NSE", "^BSESN": "BSE", "^CNXIT": "NSE"}


@dataclass(frozen=True)
class Market:
    exchange: str          # "NSE", "BSE", "US"
    currency: str          # ISO code
    currency_symbol: str
    cost_model: str        # key into market.costs.cost_model_for
    allows_overnight_short: bool
    # When a day's bar is final, in UTC, with half an hour's margin: NSE and BSE
    # close at 15:30 IST (10:00 UTC); US markets at 16:00 New York time, which is
    # 21:00 UTC in winter and 20:00 in summer.
    session_close_utc: time = time(21, 30)


US = Market("US", "USD", "$", "flat_bps", True, time(21, 30))
NSE = Market("NSE", "INR", "₹", "india_equity_delivery", False, time(10, 30))
BSE = Market("BSE", "INR", "₹", "india_equity_delivery", False, time(10, 30))


def normalize_ticker(raw: str) -> str:
    """Upper case; "BRK.B" -> "BRK-B"; exchange suffixes and indices kept."""
    symbol = (raw or "").strip().upper()
    if symbol.startswith("^") or "." not in symbol:
        return symbol
    head, _, suffix = symbol.rpartition(".")
    if len(suffix) == 1:                     # a share class, US style
        return f"{head}-{suffix}"
    return symbol


def completed_bars(bars, market: Market, now_utc: datetime):
    """Drop today's bar while its session is still open.

    During trading hours the provider returns a bar for today whose "close" is
    the latest price. A signal decided "at the close" must not use it.
    """
    if bars and bars[-1].day == now_utc.date() and now_utc.time() < market.session_close_utc:
        return bars[:-1]
    return bars


def market_for(ticker: str) -> Market:
    symbol = normalize_ticker(ticker)
    if symbol in INDIAN_INDICES:
        return NSE if INDIAN_INDICES[symbol] == "NSE" else BSE
    if "." in symbol:
        exchange = INDIAN_SUFFIXES.get(symbol.rpartition(".")[2])
        if exchange == "NSE":
            return NSE
        if exchange == "BSE":
            return BSE
    return US
