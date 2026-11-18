"""When the signal returned more than buy-and-hold but the verdict is "no
edge", the statement says why in one plain line (live-site feedback,
2026-11-18): a higher return is not an edge if randomly timed trades with the
same time in the market do as well, or if it came with worse risk-adjusted
returns. The added line states no figure the sentence does not already hold.
"""

from __future__ import annotations

import re

from market.signal_eval import EDGE_PERCENTILE, backtest_verdict


def _ev(sig_cagr, bh_cagr, sig_sharpe, bh_sharpe, pct):
    return {"ok": True, "years": 4.9, "trading_days": 1200, "start": "2021-12-01", "end": "2026-11-17",
            "random": {"signal_sharpe_percentile": pct, "runs": 200},
            "risk": {"signal": {"cagr": sig_cagr, "sharpe": sig_sharpe, "max_drawdown": -0.2, "time_in_market": 0.6},
                     "buy_and_hold": {"cagr": bh_cagr, "sharpe": bh_sharpe, "max_drawdown": -0.3}}}


def _figures(text):
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def test_higher_return_but_not_better_than_random_timing_says_so():
    # DIXON.NS on the live site: 27.3% a year against 19.1%, Sharpe 1.10 against 0.65, beat 89%.
    v = backtest_verdict("DIXON.NS", _ev(0.273, 0.191, 1.10, 0.65, 0.89))
    assert v["verdict"] == "no_edge"
    assert "random" in v["statement"].split("rule out luck)")[-1].lower()
    assert "same time in the market" in v["statement"]


def test_higher_return_with_a_worse_sharpe_says_so():
    v = backtest_verdict("X.NS", _ev(0.20, 0.15, 0.50, 0.70, 0.97))
    tail = v["statement"].split("rule out luck)")[-1]
    assert "risk" in tail.lower() and "Sharpe" in tail


def test_the_added_line_brings_no_new_figure():
    v = backtest_verdict("DIXON.NS", _ev(0.273, 0.191, 1.10, 0.65, 0.89))
    head, tail = v["statement"].split("rule out luck)")
    assert _figures(tail) <= _figures(head)


def test_no_line_when_the_signal_returned_less():
    v = backtest_verdict("NVDA", _ev(0.123, 0.372, 0.51, 0.87, 0.465))
    assert v["statement"].rstrip().endswith("rule out luck).")


def test_no_line_when_there_is_an_edge():
    v = backtest_verdict("X.NS", _ev(0.30, 0.15, 1.2, 0.7, EDGE_PERCENTILE + 0.01))
    assert v["verdict"] == "edge" and "same time in the market" not in v["statement"]
