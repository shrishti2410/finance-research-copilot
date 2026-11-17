"""What kind of market a stock is in, day by day, from its own past only.

Three measurements at each bar t, each computed from bars 0..t:

    trend strength   ADX(14), Wilder's directional index, 0 to 100
    direction        the 20-day change of the 50-day SMA, as a fraction
    volatility       20-day realised volatility (annualised), and its
                     percentile rank among the previous 252 days' values

and one label:

    volatile        volatility percentile >= 0.80: unusually turbulent for
                    this stock, whatever the trend
    trending_up     ADX >= 25 and the 50-day SMA rising
    trending_down   ADX >= 25 and the 50-day SMA falling
    ranging         ADX < 20
    transition      ADX between 20 and 25: neither clearly
    unknown         the warm-up, before every measurement exists

The thresholds are the conventional readings (Wilder's 25 for a trend, 20 for
none) and a round 80th percentile. They were fixed before the signals were
evaluated and are not tuned on the result.
"""

from __future__ import annotations

import numpy as np

from market import indicators as ind

__all__ = ["REGIMES", "RULES", "DEFAULT_RULE", "measure", "classify", "ADX_TREND",
           "ADX_RANGE", "VOL_HIGH", "SLOPE_DAYS", "VOL_WINDOW", "VOL_LOOKBACK"]

REGIMES = ("trending_up", "trending_down", "ranging", "transition", "volatile", "unknown")
ADX_PERIOD = 14
ADX_TREND = 25.0
ADX_RANGE = 20.0
SLOPE_SMA = 50
SLOPE_DAYS = 20
VOL_WINDOW = 20
VOL_LOOKBACK = 252
VOL_HIGH = 0.80

# How a strong trend's direction is read (ADX >= 25):
#   "slope"  the 50-day SMA's 20-day slope alone (the original rule)
#   "agree"  the slope and the DI lines must agree; when they disagree the
#            stock is turning, labelled "transition". Proposed 2026-11-16
#            after SBIN.NS read trending_down with +DI 38 against -DI 6; see
#            benchmarks/REGIME_RULE_CHECK.md for the hold-out test.
RULES = ("slope", "agree")
DEFAULT_RULE = "slope"


def _realised_vol(close: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(close), np.nan)
    r = np.diff(np.log(close))
    for t in range(n, len(close)):
        out[t] = np.std(r[t - n:t], ddof=1) * np.sqrt(252)
    return out


def _percentile_rank(x: np.ndarray, lookback: int) -> np.ndarray:
    """Share of the previous `lookback` values (t excluded) that are <= x[t]."""
    out = np.full(len(x), np.nan)
    for t in range(len(x)):
        window = x[max(0, t - lookback):t]
        window = window[~np.isnan(window)]
        if np.isnan(x[t]) or len(window) < lookback // 2:
            continue
        out[t] = float(np.mean(window <= x[t]))
    return out


def measure(high, low, close) -> dict[str, np.ndarray]:
    """Every measurement, as arrays aligned with the bars."""
    h, lo, c = (np.asarray(a, dtype=float) for a in (high, low, close))
    adx, plus_di, minus_di = ind.adx(h, lo, c, ADX_PERIOD)
    sma = ind.sma(c, SLOPE_SMA)
    slope = np.full(len(c), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope[SLOPE_DAYS:] = sma[SLOPE_DAYS:] / sma[:-SLOPE_DAYS] - 1.0
    vol = _realised_vol(c, VOL_WINDOW)
    return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di, "sma50_slope_20d": slope,
            "volatility": vol, "volatility_percentile": _percentile_rank(vol, VOL_LOOKBACK)}


def classify(m: dict[str, np.ndarray], rule: str = DEFAULT_RULE) -> list[str]:
    if rule not in RULES:
        raise ValueError(f"Unknown regime rule {rule!r}. Known: {', '.join(RULES)}.")
    labels = []
    for adx, slope, pct, plus, minus in zip(m["adx"], m["sma50_slope_20d"],
                                            m["volatility_percentile"], m["plus_di"],
                                            m["minus_di"]):
        if np.isnan(adx) or np.isnan(slope) or np.isnan(pct):
            labels.append("unknown")
        elif pct >= VOL_HIGH:
            labels.append("volatile")
        elif adx >= ADX_TREND and rule == "slope":
            labels.append("trending_up" if slope > 0 else "trending_down")
        elif adx >= ADX_TREND:
            if slope > 0 and plus > minus:
                labels.append("trending_up")
            elif slope < 0 and minus > plus:
                labels.append("trending_down")
            else:
                labels.append("transition")
        elif adx < ADX_RANGE:
            labels.append("ranging")
        else:
            labels.append("transition")
    return labels
