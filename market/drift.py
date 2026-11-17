"""Has a strategy stopped working the way it used to? A lower CUSUM test.

At each refit (market/signals.py), each rule's last RECENT_DAYS net daily
returns are compared with its own earlier training history:

1. Both are cut into BLOCK-day sums (weekly, roughly), which tames daily
   noise enough for the test to see a sustained change.
2. The recent blocks are standardised by the history blocks' mean and
   standard deviation: z = (block - mean) / sd.
3. A one-sided lower CUSUM (Page, 1954) accumulates shortfalls beyond an
   allowance: S = max(0, S - z - K). The rule is flagged as drifting when S
   reaches H at any point in the recent window.

K = 0.5 and H = 4 are the textbook design for detecting a sustained drop of
one standard deviation (Montgomery, Introduction to Statistical Quality
Control). They were fixed before any evaluation and not tuned.

The flag is always computed and shown. Acting on it, by halving a flagged
rule's weight, is optional (`drift_downweight`, off by default) until an
evaluation with an adoption rule written down beforehand decides
(CLAUDE.md backlog item 4's method).
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["RECENT_DAYS", "BLOCK", "K", "H", "MIN_HISTORY_DAYS", "DOWNWEIGHT",
           "lower_cusum", "assess"]

RECENT_DAYS = 60          # about three months
BLOCK = 5                 # trading days per block
K = 0.5                   # allowance, in standard deviations
H = 4.0                   # decision threshold
MIN_HISTORY_DAYS = 120    # fewer and the history's spread is not worth testing against
DOWNWEIGHT = 0.5          # the weight multiplier for a flagged rule, when acting on it


def lower_cusum(z: np.ndarray, k: float = K) -> float:
    """The largest value the lower CUSUM reaches over z."""
    s = peak = 0.0
    for value in z:
        s = max(0.0, s - float(value) - k)
        peak = max(peak, s)
    return peak


def _blocks(r: np.ndarray) -> np.ndarray:
    n = len(r) // BLOCK * BLOCK
    return r[len(r) - n:].reshape(-1, BLOCK).sum(axis=1) if n else np.array([])


def _sharpe(r: np.ndarray) -> float | None:
    sd = float(np.std(r, ddof=1)) if len(r) > 1 else 0.0
    return None if sd == 0 or math.isnan(sd) else round(float(np.mean(r) / sd * math.sqrt(252)), 4)


def assess(returns: np.ndarray, t: int, history_days: int) -> dict:
    """Drift of one rule at bar t: its last RECENT_DAYS against the
    `history_days` before them. Uses returns up to and including t only."""
    recent = returns[max(0, t - RECENT_DAYS + 1):t + 1]
    start = max(0, t - RECENT_DAYS - history_days + 1)
    history = returns[start:max(0, t - RECENT_DAYS + 1)]
    base = {"flagged": False, "cusum": 0.0, "recent_sharpe": _sharpe(recent),
            "history_sharpe": _sharpe(history), "note": None}
    if len(history) < MIN_HISTORY_DAYS or len(recent) < RECENT_DAYS:
        return {**base, "note": "not enough history to test for drift"}
    hb, rb = _blocks(history), _blocks(recent)
    sd = float(np.std(hb, ddof=1)) if len(hb) > 1 else 0.0
    if sd == 0 or math.isnan(sd):
        return {**base, "note": "no variation in the rule's history (it rarely traded)"}
    s = lower_cusum((rb - float(np.mean(hb))) / sd)
    return {**base, "cusum": round(s, 3), "flagged": s >= H}
