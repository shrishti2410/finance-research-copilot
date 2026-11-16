"""Trading costs for the backtester, by market.

A cost model prices one leg of a trade: `charges(side, notional)` returns the
money paid, itemised. Slippage is separate (market/backtest.py) because it is
a worse fill, not a fee.

Indian equity delivery (`india_equity_delivery`)
------------------------------------------------
A buy held overnight and sold later, on NSE's cash segment. The rates are the
equity-delivery column of Zerodha's published charges page
(https://zerodha.com/charges), read on 2026-11-16. That is a broker's summary
of statutory and exchange charges, not the statutes themselves: NSE's page
timed out and the Income Tax page returned 404 when they were looked up, so
the rates below are **not confirmed from an exchange or government source**.
Each is a parameter, so a confirmed rate is a one-line change.

| charge | rate | on | side |
|---|---|---|---|
| brokerage | ₹0 (Zerodha delivery) | - | both |
| STT (securities transaction tax) | 0.1% | turnover | buy and sell |
| exchange transaction charge (NSE) | 0.00307% | turnover | both |
| SEBI turnover fee | ₹10 per crore (0.0001%) | turnover | both |
| stamp duty | 0.015% | turnover | buy only |
| GST | 18% | brokerage + exchange charge + SEBI fee | both |
| DP (depository) charge | ₹15.34 per stock per sell day, incl. GST | flat | sell only |

Zerodha's page states the DP charge as "₹3.5 CDSL fee + ₹9.5 Zerodha fee +
₹2.34 GST"; it is per scrip per day, irrespective of quantity. Brokerage for
companies, trusts and HUFs is 0.1% or ₹20, whichever is less, per the same
page; this model is for an individual.

Shorting: equity delivery cannot be held short overnight on the cash segment,
so the Indian model is long or flat only (market.markets.Market).

Flat basis points (`flat_bps`)
------------------------------
The original model: `cost_bps` of turnover on each side. Still the default
for US tickers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Charges", "CostModel", "FlatBps", "IndiaEquityDelivery", "cost_model_for"]


@dataclass(frozen=True)
class Charges:
    total: float
    items: dict[str, float] = field(default_factory=dict)


class CostModel:
    key = "base"
    allows_short = True

    def charges(self, side: str, notional: float) -> Charges:  # pragma: no cover
        raise NotImplementedError

    def describe(self) -> dict:  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True)
class FlatBps(CostModel):
    cost_bps: float = 5.0
    key: str = "flat_bps"
    allows_short: bool = True

    def charges(self, side: str, notional: float) -> Charges:
        fee = abs(notional) * self.cost_bps / 1e4
        return Charges(fee, {"commission": fee} if notional else {})

    def describe(self) -> dict:
        return {"model": self.key, "cost_bps_per_side": self.cost_bps}


@dataclass(frozen=True)
class IndiaEquityDelivery(CostModel):
    brokerage_rate: float = 0.0          # Zerodha: zero for delivery
    brokerage_cap: float | None = None   # e.g. 20.0 for "0.1% or ₹20, whichever is less"
    stt_rate: float = 0.001              # 0.1%, buy and sell
    exchange_rate: float = 0.0000307     # NSE 0.00307%
    sebi_rate: float = 0.000001          # ₹10 per crore
    stamp_rate: float = 0.00015          # 0.015%, buy only
    gst_rate: float = 0.18               # on brokerage + exchange + SEBI
    dp_charge: float = 15.34             # per sell, incl. GST
    source: str = "https://zerodha.com/charges (equity delivery column), read 2026-11-16"
    key: str = "india_equity_delivery"
    allows_short: bool = False

    def charges(self, side: str, notional: float) -> Charges:
        turnover = abs(notional)
        if turnover == 0:
            return Charges(0.0, {})
        brokerage = turnover * self.brokerage_rate
        if self.brokerage_cap is not None:
            brokerage = min(brokerage, self.brokerage_cap)
        exchange = turnover * self.exchange_rate
        sebi = turnover * self.sebi_rate
        items = {
            "brokerage": brokerage,
            "stt": turnover * self.stt_rate,
            "exchange": exchange,
            "sebi": sebi,
            "stamp_duty": turnover * self.stamp_rate if side == "buy" else 0.0,
            "gst": self.gst_rate * (brokerage + exchange + sebi),
            "dp_charge": self.dp_charge if side == "sell" else 0.0,
        }
        return Charges(sum(items.values()), items)

    def describe(self) -> dict:
        return {
            "model": self.key, "source": self.source,
            "stt": "0.1% buy and sell", "exchange": "0.00307% (NSE)",
            "sebi": "Rs 10 per crore", "stamp_duty": "0.015% on buys",
            "gst": "18% on brokerage, exchange and SEBI charges",
            "dp_charge": f"Rs {self.dp_charge} per sell", "brokerage": "0 (delivery)",
            "short_selling": "not available overnight in the cash segment",
        }


def cost_model_for(key: str, cost_bps: float = 5.0) -> CostModel:
    if key == "india_equity_delivery":
        return IndiaEquityDelivery()
    if key == "flat_bps":
        return FlatBps(cost_bps)
    raise KeyError(f"Unknown cost model {key!r}. Known: flat_bps, india_equity_delivery.")
