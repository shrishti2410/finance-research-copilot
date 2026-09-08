"""Tests for the groundedness checker.

The deterministic half is tested exhaustively, because it is the part that must
not be wrong -- it is what keeps the judge honest. The model half is tested only
for how its replies are parsed and applied; what it decides is not something a
test can pin.
"""

import pytest

from eval.judge import (
    Claim,
    Judgement,
    _parse_judge,
    answer_claims,
    apply_judgement,
    evidence_numbers,
    literal_support,
    render_evidence,
)

TRACE = [
    {
        "tool": "calculate_ratio",
        "arguments": {"ticker": "NVDA", "ratio_name": "gross_margin"},
        "result": {"ok": True, "value": 0.710681, "formatted": "71.07%",
                   "inputs": {"gross_profit": 153463000000.0,
                              "revenue": 215938000000.0}},
    },
    {"tool": "final_answer", "arguments": {}, "result": "..."},
]


# ── pulling numbers out of the evidence ─────────────────────────────────────

def test_evidence_collects_numbers_from_nested_results():
    numbers = evidence_numbers(TRACE)
    assert 0.710681 in numbers
    assert 153463000000.0 in numbers
    assert 215938000000.0 in numbers


def test_the_final_answer_step_is_not_evidence():
    """The answer cannot be its own support."""
    trace = [{"tool": "final_answer", "arguments": {}, "result": "It was 99.9%."}]
    assert 99.9 not in evidence_numbers(trace)


def test_booleans_are_not_numbers():
    """ok: true would otherwise become the evidence number 1.0 and ground any
    answer that mentions 1."""
    numbers = evidence_numbers([
        {"tool": "t", "arguments": {}, "result": {"ok": True, "value": 5.0}}
    ])
    assert 1.0 not in numbers
    assert 5.0 in numbers


# ── literal support ─────────────────────────────────────────────────────────

def test_a_ratio_written_as_a_percentage_is_the_same_number():
    assert literal_support(71.07, {0.710681})


def test_dollars_quoted_in_billions_are_the_same_number():
    assert literal_support(215.938, {215938000000.0})
    assert literal_support(215938.0, {215938000000.0})


def test_rounding_is_tolerated():
    assert literal_support(71.07, {0.7106812345})


def test_an_unrelated_number_is_not_supported():
    assert not literal_support(38.03, {0.710681, 215938000000.0})


def test_zero_evidence_supports_nothing():
    assert not literal_support(42.0, set())


# ── claims found in an answer ───────────────────────────────────────────────

def test_a_grounded_answer_has_every_claim_literal():
    answer = "NVIDIA's gross margin was 71.07%, on revenue of $215.9 billion."
    claims = answer_claims(answer, evidence_numbers(TRACE))
    assert claims and all(c.literal for c in claims)


def test_an_invented_number_is_not_literal():
    answer = "NVIDIA's gross margin was 71.07% and Apple's was 38.03%."
    claims = answer_claims(answer, evidence_numbers(TRACE))
    invented = [c for c in claims if abs(c.value - 38.03) < 0.001]
    assert invented and not invented[0].literal


def test_repeated_numbers_are_one_claim():
    answer = "It was 71.07%. That means 71.07% of each dollar."
    assert len(answer_claims(answer, evidence_numbers(TRACE))) == 1


def test_a_claim_quotes_its_surrounding_context():
    claims = answer_claims("The margin was 71.07% last year.", evidence_numbers(TRACE))
    assert "71.07" in claims[0].quote


# ── flagging ────────────────────────────────────────────────────────────────

def test_a_claim_is_flagged_only_when_both_checks_say_ungrounded():
    """Either check alone is not enough: the literal check cannot see derived
    values, and the judge is a model."""
    assert Claim("q", 1.0, literal=False, judged_grounded=False).flagged
    assert not Claim("q", 1.0, literal=True, judged_grounded=False).flagged
    assert not Claim("q", 1.0, literal=False, judged_grounded=True).flagged
    assert not Claim("q", 1.0, literal=False, judged_grounded=None).flagged


def test_the_judge_contradicting_the_literal_check_is_recorded_as_a_judge_error():
    claim = Claim("q", 1.0, literal=True, judged_grounded=False)
    assert claim.judge_disagrees
    assert not claim.flagged


def test_a_judgement_is_flagged_when_any_claim_is():
    judgement = Judgement("c", "q", "a", claims=[
        Claim("x", 1.0, literal=True, judged_grounded=True),
        Claim("y", 2.0, literal=False, judged_grounded=False),
    ])
    assert judgement.flagged
    assert len(judgement.flagged_claims) == 1


# ── the judge's reply ───────────────────────────────────────────────────────

def test_a_fenced_reply_parses():
    text = "```json\n{\"claims\":[{\"value\":\"71.07\",\"kind\":\"stated\"}]}\n```"
    assert _parse_judge(text) == [{"value": "71.07", "kind": "stated"}]


def test_prose_around_the_json_is_ignored():
    text = 'Sure.\n{"claims":[{"value":"1","kind":"derived"}]}\nHope that helps.'
    assert _parse_judge(text)[0]["kind"] == "derived"


def test_individual_claims_are_salvaged_from_a_broken_document():
    """A 7B model asked for JSON emits unescaped quotes. A partial ruling still
    flags what it ruled on; discarding it would silently report 'grounded'."""
    text = ('{"claims":[{"value":"5","kind":"stated","basis":"he said "five""},'
            '{"value":"6","kind":"ungrounded","basis":"none"}]}')
    salvaged = _parse_judge(text)
    assert {c["value"] for c in salvaged} == {"6"}


def test_a_reply_with_no_json_raises():
    with pytest.raises(ValueError, match="no JSON object"):
        _parse_judge("I could not determine that.")


def test_rulings_are_matched_to_claims_by_value_not_position():
    """The judge reorders and rephrases; pairing by index attaches rulings to
    the wrong claims."""
    claims = [Claim("a", 71.07, literal=True), Claim("b", 38.03, literal=False)]
    apply_judgement(claims, [
        {"value": "38.03", "kind": "ungrounded", "basis": "not in evidence"},
        {"value": "71.07", "kind": "stated", "basis": "gross_margin"},
    ])
    assert claims[0].kind == "stated" and claims[0].judged_grounded is True
    assert claims[1].kind == "ungrounded" and claims[1].judged_grounded is False
    assert claims[1].flagged


def test_a_ruling_for_an_unknown_number_is_ignored():
    claims = [Claim("a", 71.07, literal=True)]
    apply_judgement(claims, [{"value": "999", "kind": "ungrounded"}])
    assert claims[0].judged_grounded is None


# ── evidence rendering ──────────────────────────────────────────────────────

def test_rendered_evidence_shows_calls_and_results():
    rendered = render_evidence(TRACE)
    assert "calculate_ratio" in rendered
    assert "0.710681" in rendered
    assert "final_answer" not in rendered


def test_a_turn_with_no_tool_call_says_so():
    """An empty evidence block must not read as 'nothing to check'."""
    rendered = render_evidence([{"tool": "final_answer", "arguments": {}, "result": "x"}])
    assert "no tool was called" in rendered
