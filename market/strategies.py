"""Seventeen classic trading strategies behind one interface.

A strategy turns daily bars into a **target position for the next bar**:
`positions(prices)[t]` is the exposure wanted after seeing bar t's close
(1 long, 0 flat, -1 short), and the backtester (market/backtest.py) puts it
on at bar t+1's open. A strategy only ever reads bars 0..t to decide t: the
causality test in tests/test_backtest.py runs every strategy on truncated
history and checks that no past position changes when the future is added.

The strategies here are textbook versions, written for clarity, not tuned.
Each one's default parameters are the common ones from its literature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from market import indicators as ind

__all__ = ["Prices", "Strategy", "STRATEGIES", "get_strategy"]


@dataclass(frozen=True)
class Prices:
    """Adjusted OHLCV arrays, oldest first. See market/backtest.py for why adjusted."""

    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    def __len__(self) -> int:
        return len(self.close)


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str
    description: str
    params: dict[str, float]                 # defaults
    rule: Callable[..., np.ndarray] = field(repr=False)
    allows_short: bool = False

    def positions(self, prices: Prices, **overrides) -> np.ndarray:
        params = {**self.params, **{k: v for k, v in overrides.items() if k in self.params}}
        out = np.asarray(self.rule(prices, **params), dtype=float)
        out = np.nan_to_num(out, nan=0.0)
        if not self.allows_short:
            out = np.clip(out, 0.0, 1.0)
        return out


def _hold(entry: np.ndarray, exit_: np.ndarray) -> np.ndarray:
    """Long from an entry signal until an exit signal: a stateful position."""
    out = np.zeros(len(entry))
    holding = False
    for t in range(len(entry)):
        if not holding and entry[t]:
            holding = True
        elif holding and exit_[t]:
            holding = False
        out[t] = 1.0 if holding else 0.0
    return out


def _gt(a, b) -> np.ndarray:
    """a > b where both are defined; False during warm-up."""
    with np.errstate(invalid="ignore"):
        return np.where(np.isnan(a) | np.isnan(b), False, a > b)


def _lt(a, b) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return np.where(np.isnan(a) | np.isnan(b), False, a < b)


def buy_and_hold(p: Prices) -> np.ndarray:
    return np.ones(len(p))


def sma_cross(p: Prices, fast: float = 50, slow: float = 200) -> np.ndarray:
    return _gt(ind.sma(p.close, int(fast)), ind.sma(p.close, int(slow))).astype(float)


def ema_cross(p: Prices, fast: float = 12, slow: float = 26) -> np.ndarray:
    return _gt(ind.ema(p.close, int(fast)), ind.ema(p.close, int(slow))).astype(float)


def price_above_sma(p: Prices, period: float = 200) -> np.ndarray:
    return _gt(p.close, ind.sma(p.close, int(period))).astype(float)


def macd_cross(p: Prices, fast: float = 12, slow: float = 26, signal: float = 9) -> np.ndarray:
    line, sig, _ = ind.macd(p.close, int(fast), int(slow), int(signal))
    return _gt(line, sig).astype(float)


def rsi_reversion(p: Prices, period: float = 14, buy_below: float = 30,
                  sell_above: float = 70) -> np.ndarray:
    r = ind.rsi(p.close, int(period))
    return _hold(_lt(r, np.full(len(r), buy_below)), _gt(r, np.full(len(r), sell_above)))


def bollinger_reversion(p: Prices, period: float = 20, width: float = 2.0) -> np.ndarray:
    middle, _, lower = ind.bollinger(p.close, int(period), width)
    return _hold(_lt(p.close, lower), _gt(p.close, middle))


def bollinger_breakout(p: Prices, period: float = 20, width: float = 2.0) -> np.ndarray:
    middle, upper, _ = ind.bollinger(p.close, int(period), width)
    return _hold(_gt(p.close, upper), _lt(p.close, middle))


def _rolling_extreme(x: np.ndarray, n: int, fn) -> np.ndarray:
    """fn over the n bars *before* t (t excluded), NaN until n bars exist."""
    out = np.full(len(x), np.nan)
    for t in range(n, len(x)):
        out[t] = fn(x[t - n:t])
    return out


def donchian_breakout(p: Prices, entry: float = 20, exit: float = 10) -> np.ndarray:
    """The Turtle rule: buy a close above the prior 20-day high, sell one below
    the prior 10-day low."""
    upper = _rolling_extreme(p.high, int(entry), np.max)
    lower = _rolling_extreme(p.low, int(exit), np.min)
    return _hold(_gt(p.close, upper), _lt(p.close, lower))


def momentum(p: Prices, lookback: float = 252, skip: float = 21) -> np.ndarray:
    """Time-series momentum: long while the return from `lookback` to `skip`
    days ago is positive (the 12-1 month rule)."""
    n, s = int(lookback), int(skip)
    out = np.zeros(len(p))
    c = p.close
    for t in range(n, len(c)):
        out[t] = 1.0 if c[t - s] / c[t - n] - 1.0 > 0 else 0.0
    return out


def zscore_reversion(p: Prices, period: float = 20, entry: float = -2.0,
                     exit: float = 0.0) -> np.ndarray:
    mean = ind.sma(p.close, int(period))
    _, upper, _ = ind.bollinger(p.close, int(period), 1.0)
    std = upper - mean
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (p.close - mean) / std
    return _hold(_lt(z, np.full(len(z), entry)), _gt(z, np.full(len(z), exit)))


def atr_trailing_stop(p: Prices, trend: float = 50, atr_period: float = 22,
                      multiple: float = 3.0) -> np.ndarray:
    """Enter above the 50-day SMA; exit when the close falls more than three
    ATRs below the highest close since entry (a chandelier exit)."""
    trend_line = ind.sma(p.close, int(trend))
    a = ind.atr(p.high, p.low, p.close, int(atr_period))
    out = np.zeros(len(p))
    holding, peak = False, 0.0
    for t in range(len(p)):
        c = p.close[t]
        if not holding:
            if not np.isnan(trend_line[t]) and not np.isnan(a[t]) and c > trend_line[t]:
                holding, peak = True, c
        else:
            peak = max(peak, c)
            if c < peak - multiple * a[t]:
                holding = False
        out[t] = 1.0 if holding else 0.0
    return out


def sma_cross_long_short(p: Prices, fast: float = 50, slow: float = 200) -> np.ndarray:
    f, s = ind.sma(p.close, int(fast)), ind.sma(p.close, int(slow))
    return np.where(np.isnan(f) | np.isnan(s), 0.0, np.where(f > s, 1.0, -1.0))


def rsi2_pullback(p: Prices, period: float = 2, buy_below: float = 10,
                  trend: float = 200, exit_sma: float = 5) -> np.ndarray:
    """A short-term pullback inside a long-term uptrend: buy when a 2-day RSI
    drops below 10 while the close is above its 200-day SMA, sell when the
    close recovers above its 5-day SMA."""
    r = ind.rsi(p.close, int(period))
    uptrend = _gt(p.close, ind.sma(p.close, int(trend)))
    entry = _lt(r, np.full(len(r), buy_below)) & uptrend
    return _hold(entry, _gt(p.close, ind.sma(p.close, int(exit_sma))))


def keltner_breakout(p: Prices, period: float = 20, multiple: float = 2.0) -> np.ndarray:
    """Buy a close above the upper Keltner channel (EMA + 2 ATR); sell a close
    below the middle line."""
    middle, upper, _ = ind.keltner(p.high, p.low, p.close, int(period), multiple)
    return _hold(_gt(p.close, upper), _lt(p.close, middle))


def adx_trend(p: Prices, period: float = 14, threshold: float = 25) -> np.ndarray:
    """Long while ADX says a strong trend (above 25) and +DI above -DI says it
    is up; flat otherwise."""
    strength, plus_di, minus_di = ind.adx(p.high, p.low, p.close, int(period))
    strong = _gt(strength, np.full(len(p), threshold))
    return (strong & _gt(plus_di, minus_di)).astype(float)


def trend_alignment(p: Prices, short: float = 20, medium: float = 50,
                    long: float = 200) -> np.ndarray:
    """Long while close > SMA 20 > SMA 50 > SMA 200: every horizon agrees."""
    s, m, lg = (ind.sma(p.close, int(n)) for n in (short, medium, long))
    return (_gt(p.close, s) & _gt(s, m) & _gt(m, lg)).astype(float)


def obv_trend(p: Prices, period: float = 50) -> np.ndarray:
    """Long while on-balance volume is above its 50-day average and the close
    above its own: price rising on volume."""
    o = ind.obv(p.close, p.volume)
    n = int(period)
    return (_gt(o, ind.sma(o, n)) & _gt(p.close, ind.sma(p.close, n))).astype(float)


STRATEGIES: dict[str, Strategy] = {s.key: s for s in [
    Strategy("buy_and_hold", "Buy and hold",
             "Long every day: the benchmark every other strategy is compared with.",
             {}, buy_and_hold),
    Strategy("sma_cross", "Moving-average crossover (golden cross)",
             "Long while the fast SMA is above the slow SMA.",
             {"fast": 50, "slow": 200}, sma_cross),
    Strategy("ema_cross", "EMA crossover",
             "Long while the fast EMA is above the slow EMA.",
             {"fast": 12, "slow": 26}, ema_cross),
    Strategy("price_above_sma", "Trend filter",
             "Long while the close is above its moving average.",
             {"period": 200}, price_above_sma),
    Strategy("macd_cross", "MACD signal crossover",
             "Long while the MACD line is above its signal line.",
             {"fast": 12, "slow": 26, "signal": 9}, macd_cross),
    Strategy("rsi_reversion", "RSI mean reversion",
             "Buy when RSI falls below 30, sell when it rises above 70.",
             {"period": 14, "buy_below": 30, "sell_above": 70}, rsi_reversion),
    Strategy("bollinger_reversion", "Bollinger mean reversion",
             "Buy a close below the lower band, sell when it recovers above the middle.",
             {"period": 20, "width": 2.0}, bollinger_reversion),
    Strategy("bollinger_breakout", "Bollinger breakout",
             "Buy a close above the upper band, sell when it falls below the middle.",
             {"period": 20, "width": 2.0}, bollinger_breakout),
    Strategy("donchian_breakout", "Donchian channel breakout (Turtle)",
             "Buy a close above the prior 20-day high, sell one below the prior 10-day low.",
             {"entry": 20, "exit": 10}, donchian_breakout),
    Strategy("momentum", "Time-series momentum (12-1)",
             "Long while the return from 12 months to 1 month ago is positive.",
             {"lookback": 252, "skip": 21}, momentum),
    Strategy("zscore_reversion", "Z-score mean reversion",
             "Buy two standard deviations below the 20-day mean, sell back at the mean.",
             {"period": 20, "entry": -2.0, "exit": 0.0}, zscore_reversion),
    Strategy("atr_trailing_stop", "Trend with an ATR trailing stop",
             "Enter above the 50-day SMA; exit three ATRs below the highest close since entry.",
             {"trend": 50, "atr_period": 22, "multiple": 3.0}, atr_trailing_stop),
    Strategy("sma_cross_long_short", "Moving-average crossover, long and short",
             "Long while the fast SMA is above the slow one, short while it is below.",
             {"fast": 50, "slow": 200}, sma_cross_long_short, allows_short=True),
    Strategy("rsi2_pullback", "RSI(2) pullback in an uptrend",
             "Buy a 2-day RSI below 10 above the 200-day SMA; sell above the 5-day SMA.",
             {"period": 2, "buy_below": 10, "trend": 200, "exit_sma": 5}, rsi2_pullback),
    Strategy("keltner_breakout", "Keltner channel breakout",
             "Buy a close above EMA 20 + 2 ATR; sell a close below EMA 20.",
             {"period": 20, "multiple": 2.0}, keltner_breakout),
    Strategy("adx_trend", "ADX trend strength",
             "Long while ADX(14) is above 25 and +DI is above -DI.",
             {"period": 14, "threshold": 25}, adx_trend),
    Strategy("trend_alignment", "Moving-average alignment",
             "Long while close > SMA 20 > SMA 50 > SMA 200.",
             {"short": 20, "medium": 50, "long": 200}, trend_alignment),
    Strategy("obv_trend", "On-balance volume trend",
             "Long while OBV and the close are both above their 50-day averages.",
             {"period": 50}, obv_trend),
]}


def get_strategy(key: str) -> Strategy:
    try:
        return STRATEGIES[key]
    except KeyError:
        raise KeyError(f"Unknown strategy {key!r}. Known: {', '.join(STRATEGIES)}.") from None
