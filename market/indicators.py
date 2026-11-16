"""Technical indicators over daily bars, in NumPy.

Every function takes plain arrays and returns arrays of the same length, with
NaN where the indicator is not yet defined (its warm-up). Written in NumPy
loops and cumulative sums rather than pandas, so that tests/test_indicators.py
can check each one against an independent pandas calculation.

Conventions, stated because charting packages differ on them
------------------------------------------------------------
- **EMA**: smoothing factor 2 / (n + 1), started from the first value and
  shown from the n-th bar on (pandas `ewm(span=n, adjust=False)`). TradingView
  starts it from the SMA of the first n bars instead; the two converge within
  a few multiples of n.
- **Wilder's smoothing** (RSI, ATR): factor 1 / n, started from the first value,
  shown from bar n (pandas `ewm(alpha=1/n, adjust=False)`). Same caveat.
- **Bollinger Bands**: SMA(20) plus and minus 2 *population* standard
  deviations (ddof=0), which is how John Bollinger and TradingView define it.
- **RSI** is 100 when there were no losses in the window, 0 when no gains.
- Indicators run on the traded (split-adjusted) prices; returns elsewhere use
  the dividend-adjusted close. See market/providers.py.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "sma", "ema", "wilder", "rsi", "macd", "bollinger", "true_range", "atr",
    "volume_sma", "obv", "adx", "keltner", "INDICATORS", "compute",
]


def _array(values) -> np.ndarray:
    return np.asarray(values, dtype=float)


def sma(values, n: int) -> np.ndarray:
    """Simple moving average over the last `n` values."""
    x = _array(values)
    out = np.full(x.shape, np.nan)
    if n < 1 or len(x) < n:
        return out
    csum = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1:] = (csum[n:] - csum[:-n]) / n
    return out


def _smooth(values, alpha: float, n: int) -> np.ndarray:
    """Exponential smoothing from the first value, NaN before bar n."""
    x = _array(values)
    out = np.full(x.shape, np.nan)
    if len(x) == 0:
        return out
    level = x[0]
    smoothed = np.empty_like(x)
    for i, value in enumerate(x):
        level = value if i == 0 else alpha * value + (1 - alpha) * level
        smoothed[i] = level
    out[n - 1:] = smoothed[n - 1:]
    return out


def ema(values, n: int) -> np.ndarray:
    return _smooth(values, 2.0 / (n + 1), n)


def wilder(values, n: int) -> np.ndarray:
    return _smooth(values, 1.0 / n, n)


def rsi(close, n: int = 14) -> np.ndarray:
    """Relative Strength Index with Wilder's smoothing, 0 to 100."""
    c = _array(close)
    out = np.full(c.shape, np.nan)
    if len(c) <= n:
        return out
    change = np.diff(c)
    gains = np.where(change > 0, change, 0.0)
    losses = np.where(change < 0, -change, 0.0)
    avg_gain, avg_loss = wilder(gains, n), wilder(losses, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        value = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    value = np.where(avg_loss == 0, np.where(avg_gain == 0, np.nan, 100.0), value)
    out[1:] = value
    return out


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    """(MACD line, signal line, histogram)."""
    c = _array(close)
    line = ema(c, fast) - ema(c, slow)
    valid = ~np.isnan(line)
    sig = np.full(c.shape, np.nan)
    if valid.sum() >= signal:
        first = int(np.argmax(valid))
        sig[first:] = ema(line[first:], signal)
    return line, sig, line - sig


def bollinger(close, n: int = 20, k: float = 2.0):
    """(middle, upper, lower): SMA(n) and k population standard deviations."""
    c = _array(close)
    middle = sma(c, n)
    sq_mean = sma(c * c, n)
    std = np.sqrt(np.maximum(sq_mean - middle * middle, 0.0))
    return middle, middle + k * std, middle - k * std


def true_range(high, low, close) -> np.ndarray:
    h, lo, c = _array(high), _array(low), _array(close)
    prev = np.concatenate(([np.nan], c[:-1]))
    ranges = np.vstack([h - lo, np.abs(h - prev), np.abs(lo - prev)])
    out = np.nanmax(ranges, axis=0)
    return out


def atr(high, low, close, n: int = 14) -> np.ndarray:
    """Average True Range with Wilder's smoothing."""
    return wilder(true_range(high, low, close), n)


def volume_sma(volume, n: int = 20) -> np.ndarray:
    return sma(volume, n)


def obv(close, volume) -> np.ndarray:
    """On-balance volume: volume added on up days, subtracted on down days."""
    c, v = _array(close), _array(volume)
    direction = np.sign(np.diff(c, prepend=c[0]))
    return np.cumsum(direction * v)


def adx(high, low, close, n: int = 14):
    """(ADX, +DI, -DI): Wilder's directional movement system, 0 to 100.

    +DM is today's high minus yesterday's when that rise is larger than the
    fall in the low, and positive; -DM the reverse. Both, and the true range,
    are smoothed with Wilder's average; +DI and -DI are the smoothed moves as a
    percentage of the smoothed range. DX is their difference over their sum,
    and ADX is DX smoothed again: how strongly the price is trending, whichever
    way. Undefined (NaN) for the first 2n-1 bars.
    """
    h, lo, c = _array(high), _array(low), _array(close)
    out = np.full(c.shape, np.nan)
    if len(c) < 2 * n:
        return out, out.copy(), out.copy()
    up = np.diff(h, prepend=h[0])
    down = -np.diff(lo, prepend=lo[0])
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = wilder(true_range(h, lo, c), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * wilder(plus_dm, n) / tr
        minus_di = 100.0 * wilder(minus_dm, n) / tr
        total = plus_di + minus_di
        dx = np.where(total > 0, 100.0 * np.abs(plus_di - minus_di) / total, 0.0)
    dx = np.where(np.isnan(plus_di), np.nan, dx)
    first = int(np.argmax(~np.isnan(dx)))
    out[first:] = wilder(dx[first:], n)
    return out, plus_di, minus_di


def keltner(high, low, close, n: int = 20, k: float = 2.0):
    """(middle, upper, lower): EMA(n) of the close, plus and minus k ATR(n)."""
    middle = ema(close, n)
    width = k * atr(high, low, close, n)
    return middle, middle + width, middle - width


# Name -> what the chart page and the agent can ask for. Each entry says how
# to compute it from bars and where it is drawn.
INDICATORS: dict[str, dict] = {
    "sma20": {"label": "SMA 20", "pane": "price"},
    "sma50": {"label": "SMA 50", "pane": "price"},
    "sma200": {"label": "SMA 200", "pane": "price"},
    "ema12": {"label": "EMA 12", "pane": "price"},
    "ema26": {"label": "EMA 26", "pane": "price"},
    "bollinger": {"label": "Bollinger 20, 2", "pane": "price"},
    "rsi14": {"label": "RSI 14", "pane": "rsi"},
    "macd": {"label": "MACD 12, 26, 9", "pane": "macd"},
    "atr14": {"label": "ATR 14", "pane": "atr"},
    "volume": {"label": "Volume", "pane": "volume"},
    "obv": {"label": "OBV", "pane": "obv"},
}


def compute(name: str, bars) -> dict[str, np.ndarray]:
    """Series for one named indicator over a list of market.providers.Bar."""
    close = [b.close for b in bars]
    if name.startswith("sma") and name[3:].isdigit():
        return {name: sma(close, int(name[3:]))}
    if name.startswith("ema") and name[3:].isdigit():
        return {name: ema(close, int(name[3:]))}
    if name == "bollinger":
        middle, upper, lower = bollinger(close)
        return {"bb_middle": middle, "bb_upper": upper, "bb_lower": lower}
    if name == "rsi14":
        return {"rsi14": rsi(close, 14)}
    if name == "macd":
        line, signal, hist = macd(close)
        return {"macd": line, "macd_signal": signal, "macd_hist": hist}
    if name == "atr14":
        return {"atr14": atr([b.high for b in bars], [b.low for b in bars], close, 14)}
    if name == "volume":
        return {"volume": _array([b.volume for b in bars]),
                "volume_sma20": volume_sma([b.volume for b in bars], 20)}
    if name == "obv":
        return {"obv": obv(close, [b.volume for b in bars])}
    raise KeyError(f"Unknown indicator {name!r}. Known: {', '.join(INDICATORS)}.")
