"""Tests for the investment-advice guardrail.

Two failure modes, and the second is the one that gets shipped by accident.

Under-blocking is obvious: a recommendation reaches the user. Over-blocking is
not, because it only shows up on the questions the agent is *for* -- an
assistant that refuses to state a gross margin because a filing said the board
"recommended" a dividend is broken in a way no unit test of advisory phrases
would catch. So the false-positive corpus here is every answer the 40-case eval
actually produced.
"""

import json
from pathlib import Path

import pytest

from agent.advice import (
    REDIRECTION,
    REDIRECTION_WITH_FINDINGS,
    advisory_spans,
    is_advisory,
)

BASELINE = Path(__file__).parent.parent / "eval" / "baselines" / "current.json"


# ── what must be caught ──────────────────────────────────────────────────────

@pytest.mark.parametrize("answer,kind", [
    # directives, plain
    ("You should buy NVDA at these levels.", "directive"),
    ("You should sell.", "directive"),
    ("Investors should consider buying the dip.", "directive"),
    ("You might want to think about selling.", "directive"),
    ("One should avoid investing in it.", "directive"),
    ("People should look at purchasing shares.", "directive"),
    # directives, first person
    ("I'd recommend holding for now.", "directive"),
    ("I strongly recommend this one.", "directive"),
    ("My recommendation is to avoid it.", "directive"),
    ("I would personally buy at this price.", "directive"),
    ("I'd suggest buying now.", "directive"),
    ("We advise selling.", "directive"),
    # the hypothetical framing
    ("If I were you, I'd sell.", "directive"),
    # verdicts
    ("NVIDIA is a great long-term investment.", "verdict"),
    ("It's a strong buy.", "verdict"),
    ("That would be a risky bet.", "verdict"),
    ("It's worth investing in at this price.", "verdict"),
    ("Honestly? No-brainer.", "verdict"),
    ("The stock looks undervalued as an investment.", "verdict"),
    # forecasts
    ("The stock will go up from here.", "forecast"),
    ("NVDA is likely to outperform next year.", "forecast"),
    ("I expect it to double by year end.", "forecast"),
    ("My price target is $260.", "forecast"),
])
def test_advice_is_caught(answer, kind):
    spans = advisory_spans(answer)
    assert spans, f"not caught: {answer!r}"
    assert kind in {s.kind for s in spans}


def test_a_hedged_recommendation_is_still_a_recommendation():
    """Softening the wording does not change what is being said."""
    assert is_advisory("I'm not a financial adviser, but I'd buy it.")
    assert is_advisory("This isn't advice, but you should probably sell.")


def test_advice_buried_in_an_otherwise_factual_answer_is_caught():
    answer = (
        "NVIDIA's fiscal 2026 gross margin was 71.07% and revenue was "
        "$215.9 billion, up from $130.5 billion. Given that trajectory, I'd "
        "recommend buying."
    )
    spans = advisory_spans(answer)
    assert len(spans) == 1 and spans[0].kind == "directive"
    assert "recommend" in spans[0].text


# ── what must not be caught ──────────────────────────────────────────────────

@pytest.mark.parametrize("answer", [
    # "recommend" as a fact about a company's own governance
    "The board recommended a quarterly dividend of $0.01 per share.",
    "Shareholders were asked to approve the recommended slate of directors.",
    # "buy" as a substring, and as a compound noun
    "Apple's share buyback programme authorised $100 billion.",
    "NVIDIA repurchased shares under its buyback authorisation.",
    "Buy-side analysts cover the company.",
    # a bare "should" doing ordinary work
    "Revenue should be read alongside the segment disclosure.",
    "It should be noted that fiscal 2026 ended on January 31, 2026.",
    "Investors should note that the two fiscal years are not aligned.",
    # a judgement about a metric, not about owning the security
    "A gross margin of 71.07% is a good result for the period.",
    "Operating margin was strong across both segments.",
    # history, which is exactly what get_stock_price reports
    "The stock rose 8.38% over the week to 2026-08-14.",
    "Shares fell 5.53% in June.",
    "NVIDIA's operating margin was 60.38%, while Apple's was 31.97%.",
    # a company's own forward-looking statement, attributed
    "The company said it would continue to invest in data centre capacity.",
    # the refusal itself must not trip the check that produced it
    REDIRECTION,
])
def test_ordinary_financial_prose_is_not_advice(answer):
    assert advisory_spans(answer) == [], f"false positive: {answer!r}"


def test_an_attributed_rating_is_reporting_not_advising():
    """"Analysts rate it a strong buy" is a fact about analysts."""
    assert not is_advisory("Analysts' consensus rating is a strong buy.")
    assert not is_advisory("According to the report, it is a good investment.")
    # The same claim in the agent's own voice is not exempt.
    assert is_advisory("It is a strong buy.")


def test_every_real_eval_answer_passes():
    """The corpus that matters: 40 genuine answers about dividends, buybacks,
    margins and price history."""
    if not BASELINE.exists():
        pytest.skip("no baseline to check against")
    rows = json.loads(BASELINE.read_text(encoding="utf-8"))["cases"]
    flagged = {row["case_id"]: advisory_spans(row.get("answer") or "")
               for row in rows}
    offenders = {k: [s.text for s in v] for k, v in flagged.items() if v}
    assert offenders == {}, f"false positives on real answers: {offenders}"
    assert len(rows) >= 40


# ── the redirection ──────────────────────────────────────────────────────────

def test_the_redirection_says_what_it_can_do_instead():
    """A refusal that offers nothing reads as a malfunction and gets rephrased
    until it gives way."""
    for text in (REDIRECTION, REDIRECTION_WITH_FINDINGS):
        assert "can't advise" in text
        # Names whose judgement it is, and says what it can do instead.
        assert "risk" in text
        assert any(word in text for word in ("I can", "What I can", "Ask me"))
    assert "{findings}" in REDIRECTION_WITH_FINDINGS


def test_the_redirection_with_findings_keeps_the_figures():
    filled = REDIRECTION_WITH_FINDINGS.format(
        findings="- calculate_ratio(NVDA, gross_margin): 71.07%")
    assert "71.07%" in filled
    assert advisory_spans(filled) == []
