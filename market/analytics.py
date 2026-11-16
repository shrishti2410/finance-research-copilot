"""Price analytics: returns, volatility, drawdown, beta, correlation.

All of these run on the **dividend-adjusted** close (`Bar.adj_close`): a
return computed from the traded close shows every ex-dividend day as a loss.

Conventions
-----------
- Daily simple returns r_t = P_t / P_{t-1} - 1.
- Annualised with 252 trading days: volatility = std(r, ddof=1) * sqrt(252);
  annualised return = (P_last / P_first) ** (252 / days) - 1, with `days`
  the number of daily returns.
- Max drawdown: the largest fall from a running peak, as a negative fraction,
  with the dates of the peak and the trough.
- Beta and correlation use the days both series traded (an inner join on the
  date), so a holiday in one market does not shift the other by a day.
- Sharpe ratio with a zero risk-free rate unless one is given (annual).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date

import numpy as np

__all__ = [
    "simple_returns", "log_returns", "volatility", "annualised_return",
    "max_drawdown", "beta_and_correlation", "summary", "TRADING_DAYS",
]

TRADING_DAYS = 252


def simple_returns(prices) -> np.ndarray:
    p = np.asarray(prices, dtype=float)
    return p[1:] / p[:-1] - 1.0


def log_returns(prices) -> np.ndarray:
    p = np.asarray(prices, dtype=float)
    return np.log(p[1:] / p[:-1])


def volatility(returns, annualise: bool = True) -> float:
    r = np.asarray(returns, dtype=float)
    if len(r) < 2:
        return float("nan")
    daily = float(np.std(r, ddof=1))
    return daily * math.sqrt(TRADING_DAYS) if annualise else daily


def annualised_return(prices) -> float:
    p = np.asarray(prices, dtype=float)
    if len(p) < 2 or p[0] <= 0:
        return float("nan")
    return float((p[-1] / p[0]) ** (TRADING_DAYS / (len(p) - 1)) - 1.0)


@dataclass(frozen=True)
class Drawdown:
    max_drawdown: float          # e.g. -0.35 for a 35% fall
    peak_day: date | None
    trough_day: date | None


def max_drawdown(prices, days=None) -> Drawdown:
    p = np.asarray(prices, dtype=float)
    if len(p) == 0:
        return Drawdown(float("nan"), None, None)
    running_peak = np.maximum.accumulate(p)
    drawdowns = p / running_peak - 1.0
    trough = int(np.argmin(drawdowns))
    peak = int(np.argmax(p[: trough + 1]))
    pick = (lambda i: days[i]) if days is not None else (lambda i: None)
    return Drawdown(float(drawdowns[trough]), pick(peak), pick(trough))


def beta_and_correlation(asset: dict, benchmark: dict) -> tuple[float, float, int]:
    """(beta, correlation, overlapping return days) from {day: adjusted close}.

    Returns are computed on the overlapping days only, in date order.
    """
    common = sorted(set(asset) & set(benchmark))
    if len(common) < 3:
        return float("nan"), float("nan"), max(len(common) - 1, 0)
    a = simple_returns([asset[d] for d in common])
    b = simple_returns([benchmark[d] for d in common])
    variance = float(np.var(b, ddof=1))
    covariance = float(np.cov(a, b, ddof=1)[0, 1])
    beta = covariance / variance if variance > 0 else float("nan")
    correlation = float(np.corrcoef(a, b)[0, 1])
    return beta, correlation, len(a)


def _clean(value):
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return round(value, 6) if isinstance(value, float) else value


def summary(bars, benchmark_bars=None, risk_free: float = 0.0) -> dict:
    """Every analytic above for a list of market.providers.Bar, as plain values."""
    if len(bars) < 2:
        raise ValueError("at least two bars are needed for returns")
    days = [b.day for b in bars]
    adj = [b.adj_close for b in bars]
    r = simple_returns(adj)
    vol = volatility(r)
    ann = annualised_return(adj)
    excess = ann - risk_free
    dd = max_drawdown(adj, days)
    out = {
        "start": days[0].isoformat(), "end": days[-1].isoformat(), "trading_days": len(bars),
        "total_return": float(adj[-1] / adj[0] - 1.0),
        "annualised_return": ann,
        "annualised_volatility": vol,
        "sharpe_ratio": excess / vol if vol and vol > 0 else float("nan"),
        "risk_free_rate": risk_free,
        "best_day": float(np.max(r)), "worst_day": float(np.min(r)),
        **{k: (v.isoformat() if isinstance(v, date) else v) for k, v in asdict(dd).items()},
        "price_basis": "dividend-adjusted close",
    }
    if benchmark_bars:
        beta, corr, n = beta_and_correlation(
            {b.day: b.adj_close for b in bars}, {b.day: b.adj_close for b in benchmark_bars})
        out.update(beta=beta, correlation=corr, overlapping_return_days=n)
    return {k: _clean(v) for k, v in out.items()}
