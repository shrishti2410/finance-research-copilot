"""Where daily prices come from, behind one small interface.

`PriceProvider` is the seam: anything with a `name` and a `daily_bars(ticker,
start, end)` that returns `Bar`s can replace Yahoo Finance. The cache
(`market/store.py`), the indicators and the analytics never import a provider
directly; they take whatever `market.store.MarketData` was built with.

Prices: what each field is
--------------------------
A bar carries both the traded prices and the adjusted close, because they
answer different questions:

- `open`, `high`, `low`, `close` are split-adjusted but **not** dividend-adjusted:
  the price a chart should show, comparable across a split.
- `adj_close` is also adjusted for dividends: the series a *return* has to be
  computed from, or every ex-dividend day looks like a loss.

Indicators run on the traded prices (a moving average of what the market
printed); returns, volatility, drawdown and beta run on `adj_close`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Bar", "PriceProvider", "YFinanceProvider", "ProviderError"]


@dataclass(frozen=True)
class Bar:
    """One trading day."""

    day: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: int


class ProviderError(RuntimeError):
    """The provider could not be reached or returned nothing usable."""


@runtime_checkable
class PriceProvider(Protocol):
    name: str

    def daily_bars(self, ticker: str, start: date, end: date) -> list[Bar]:
        """Daily bars for `ticker` from `start` to `end`, both inclusive, oldest
        first. An empty list means the provider has no bars in that range."""
        ...


class YFinanceProvider:
    """Daily bars from Yahoo Finance through yfinance.

    yfinance is Apache-2.0; its README says Yahoo's API is "intended for
    personal use only", which is the terms a public deployment runs under.
    The network policy (timeouts, retries) is the one in tools/_yahoo.py.
    """

    name = "yfinance"

    def daily_bars(self, ticker: str, start: date, end: date) -> list[Bar]:
        import yfinance as yf

        from tools._yahoo import history_timeout

        try:
            frame = yf.Ticker(ticker).history(
                start=start.isoformat(),
                # yfinance's end is exclusive; ours is inclusive.
                end=(end + timedelta(days=1)).isoformat(),
                interval="1d",
                # Both price kinds: see the module docstring.
                auto_adjust=False,
                actions=False,
                timeout=history_timeout(),
            )
        except Exception as exc:  # noqa: BLE001 - network, parsing, schema drift
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc
        if frame is None or frame.empty:
            return []
        bars = []
        for stamp, row in frame.iterrows():
            values = [row.get(k) for k in ("Open", "High", "Low", "Close", "Adj Close")]
            if any(v is None or v != v for v in values):     # NaN: a holiday row
                continue
            bars.append(Bar(
                day=stamp.date(), open=float(values[0]), high=float(values[1]),
                low=float(values[2]), close=float(values[3]), adj_close=float(values[4]),
                volume=int(row.get("Volume") or 0),
            ))
        return bars
