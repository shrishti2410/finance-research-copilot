"""Price predictions are advice too: the forecast patterns cover how they are
actually phrased.

The advice guard's forecasts needed a pronoun or "the stock" as subject and
"will/should + rise/fall": it missed a ticker or the price as the subject
("SBIN could reach ₹1,200"), being poised for a move, stated upside, further
gains ahead, a bullish outlook, and the signal described as a prediction. The
signal is a vote of rules on past prices; saying it predicts the price is the
claim the product must not make.

Ordinary prose stays clear: past moves, the signal's own wording, option
payoffs explained conditionally, and guidance attributed to the company.
"""

from __future__ import annotations

import pytest

from agent.advice import advisory_spans, is_advisory

pytestmark = pytest.mark.safety


@pytest.mark.parametrize("answer", [
    "SBIN could reach ₹1,200 in the coming weeks.",
    "SBIN.NS will likely go up from here.",
    "NVDA is likely to rebound after this pullback.",
    "The share price is poised for a rally.",
    "The stock looks set for a breakout.",
    "The price should bounce back soon.",
    "There is upside of about 15% from current levels.",
    "Downside to ₹950 is possible if the trend breaks.",
    "Further gains are likely.",
    "The outlook for the stock is bullish.",
    "We have a bullish view for the next quarter.",
    "The signal predicts the price will rise.",
    "The signal suggests that the stock will climb.",
    "Expect the stock to rally.",
    "Its price may hit new highs.",
])
def test_price_predictions_are_caught_as_forecasts(answer):
    kinds = {a.kind for a in advisory_spans(answer)}
    assert "forecast" in kinds, answer


@pytest.mark.parametrize("answer", [
    "SBIN rose 1.66% today and closed at ₹1,108.60.",
    "The stock fell 8.19% on the day.",
    "The signal is BUY on 12 of 16 rules; the five-year backtest found no edge.",
    "The engine's score is +0.437; BUY starts at +0.25.",
    "A bull call spread profits if the stock rises above the breakeven of ₹1,114 at expiry.",
    "If the price reaches ₹1,140, the spread pays its maximum of ₹25.77.",
    "Momentum rules stay in while the price is above its 200-day average.",
    "Upside and downside capture are not computed here.",
    "According to the filing, NVIDIA expects revenue of about $65 billion next quarter.",
    "The company said revenue is expected to grow in fiscal 2027.",
    "The regime is trending down: ADX 42.4, and the 50-day average is falling.",
    "Past performance does not say where the price will go.",
    "The backtest returned -5.3% a year against 19.7% for buy-and-hold.",
])
def test_reporting_and_conditional_explanations_are_not_forecasts(answer):
    assert not is_advisory(answer), [a.text for a in advisory_spans(answer)]
