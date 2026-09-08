"""Tests for deterministic answer scoring.

The extractor is the part of the harness that can quietly make a score
meaningless -- read the wrong number out of an answer and every figure in the
report is fiction. These pin the rules it follows.
"""

import pytest

from eval.datasets import Case
from eval.metrics import (
    Outcome,
    candidates,
    extract_number,
    score_case,
    summarise,
)


def case(**overrides) -> Case:
    base = dict(
        id="c", question="q", expected_answer=71.07, unit="percent",
        tolerance=0.5, answerable_via=("calculate_ratio",), company="NVDA",
        source="s",
    )
    return Case(**{**base, **overrides})


def outcome(**overrides) -> Outcome:
    base = dict(
        case_id="c", question="q", expected=1.0, unit="percent", tolerance=0.5,
        answerable_via=("calculate_ratio",), company="NVDA", extracted=1.0,
        answer="a", passed=True, latency_ms=1000.0, tool_calls=1, iterations=2,
        prompt_tokens=100, completion_tokens=10, completed=True,
        stop_reason="final_answer",
    )
    return Outcome(**{**base, **overrides})


# ── picking the right number out of prose ───────────────────────────────────

def test_a_year_in_the_sentence_is_not_the_answer():
    """The most common real answer opens with a fiscal year, so taking the
    first number in the string would score dates."""
    answer = ("NVIDIA's gross margin for the fiscal year 2026, which ended on "
              "January 31, 2026, was 71.07%.")
    assert extract_number(answer, "percent") == 71.07


def test_a_percent_case_ignores_numbers_without_a_percent_sign():
    """'0.71' might be the ratio or might be something else; guessing turns a
    miss into a false pass."""
    assert extract_number("The margin was 0.71 of revenue.", "percent") is None
    assert extract_number("The margin was 71.07%.", "percent") == 71.07


@pytest.mark.parametrize("answer,expected", [
    ("Revenue was $215.9 billion.", 215900.0),
    ("Revenue was $215,938 million.", 215938.0),
    ("Total revenue: 215,938 (in millions).", 215938.0),
    ("Revenue was $215,938,000,000.", 215938.0),
])
def test_money_is_normalised_into_the_cases_unit(answer, expected):
    assert extract_number(answer, "USD millions") == pytest.approx(expected)


def test_a_number_written_out_in_full_is_not_read_as_millions_of_millions():
    """'$120,067,000,000' is dollars. Reading it as 120 billion *millions* made
    a wrong answer look absurd rather than merely wrong."""
    assert extract_number("Net income was $120,067,000,000.", "USD millions") == 120067.0


def test_a_small_bare_number_is_not_given_an_invented_scale():
    """'It was 4.9' cannot be a revenue in millions; refusing beats guessing."""
    assert extract_number("It was 4.9.", "USD millions") is None


def test_negative_percentages_survive():
    assert extract_number("The stock fell -5.53% in June.", "percent") == -5.53


def test_a_trillion_becomes_billions():
    assert extract_number("About $5.49 trillion.", "USD billions") == pytest.approx(5490.0)


def test_prose_without_a_number_extracts_nothing():
    assert extract_number("Roughly seventy-one percent.", "percent") is None
    assert extract_number("", "percent") is None


def test_a_percent_number_is_never_offered_to_a_money_case():
    assert extract_number("Margins were 71.07%.", "USD millions") is None


def test_candidates_keep_their_position_so_first_wins_is_defined():
    found = candidates("First 10%, then 20%.")
    assert [c.value for c in found] == [10.0, 20.0]
    assert found[0].start < found[1].start


# ── scoring ─────────────────────────────────────────────────────────────────

def test_a_value_inside_tolerance_passes():
    result = score_case(case(), "The margin was 71.5%.", **_fields())
    assert result.passed and result.verdict == "pass"


def test_a_value_outside_tolerance_is_wrong_not_missing():
    result = score_case(case(), "The margin was 46.91%.", **_fields())
    assert result.verdict == "wrong"
    assert result.extracted == 46.91


def test_an_unparseable_answer_is_no_number_not_wrong():
    """Reporting these as wrong would blame the system for the extractor."""
    result = score_case(case(), "It was roughly seventy-one percent.", **_fields())
    assert result.verdict == "no_number"
    assert result.extracted is None
    assert result.passed is False


def test_a_failed_request_is_an_error_verdict():
    result = score_case(case(), "", **{**_fields(), "error": "ReadTimeout"})
    assert result.verdict == "error"


def _fields():
    return dict(
        latency_ms=1000.0, tool_calls=1, iterations=2, prompt_tokens=10,
        completion_tokens=5, completed=True, stop_reason="final_answer",
    )


# ── aggregation ─────────────────────────────────────────────────────────────

def test_summary_counts_each_verdict_separately():
    summary = summarise("x", [
        outcome(passed=True),
        outcome(passed=False, extracted=9.0),
        outcome(passed=False, extracted=None),
        outcome(passed=False, extracted=None, error="Boom"),
    ])
    assert (summary.passed, summary.wrong, summary.no_number, summary.errors) == (1, 1, 1, 1)
    assert summary.accuracy == 25.0


def test_summary_averages_latency_and_tool_calls():
    summary = summarise("x", [
        outcome(latency_ms=1000.0, tool_calls=1),
        outcome(latency_ms=3000.0, tool_calls=3),
    ])
    assert summary.mean_latency_ms == 2000.0
    assert summary.mean_tool_calls == 2.0


def test_summary_sums_tokens():
    summary = summarise("x", [
        outcome(prompt_tokens=100, completion_tokens=10),
        outcome(prompt_tokens=200, completion_tokens=20),
    ])
    assert summary.prompt_tokens == 300
    assert summary.completion_tokens == 30
    assert summary.total_tokens == 330


def test_an_empty_group_does_not_divide_by_zero():
    summary = summarise("x", [])
    assert summary.accuracy == 0.0
    assert summary.mean_latency_ms == 0.0
