"""The agent's "Final figure:" line, and the scorer reading it before the prose.

Eval run-b (2026-11-10) had three answers whose stated figure the prose regex
missed, because it took the first plausible number instead:

    #38 nvda-implied-pe-on-fy2026-eps  stated 21.65x, scored 171.25 (the price)
    #39 aapl-implied-pe-on-fy2025-eps  stated 39.05x, scored 231.29 (a price)
    #40 nvda-vs-aapl-gross-margin-gap  stated 22.28 pp, scored 71.07 (a margin)

#38 and #39 were wrong either way -- the agent used a live TTM P/E, not the
question's -- so fixing extraction must report them as wrong *with the number
the agent actually gave*. #40 is right against the reworded case (22.28).

The answers below are the run-b answers verbatim, plus the final-figure line a
model following the new prompt would add.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent.orchestrator import FINAL_FIGURE_NONE, run_agent, split_final_figure
from eval.compare import rescore
from eval.datasets import Case
from eval.metrics import (FROM_ANSWER, FROM_DECLINED, FROM_FINAL_FIGURE,
                          extract_scored, score_case)
from tools.base import ok
from tools.registry import Registry

ANSWER_38 = (
    "At its August 7, 2026 closing price of $171.25, NVIDIA's fiscal 2026 trailing "
    "twelve-month diluted earnings per share (EPS) imply a P/E ratio of 21.65x. This "
    "ratio is based on the trailing twelve months of EPS, as the P/E ratio depends on "
    "the current share price and is only meaningful as of now."
)
ANSWER_39 = (
    "The P/E ratio for Apple's fiscal 2025, as implied by its August 7, 2026 closing "
    "price of $231.29, is 39.05x. This was calculated using the trailing twelve months "
    "earnings per share (trailing EPS) of $8.98 and the current share price of "
    "$350.655. Note that the P/E ratio is based on the current share price and thus "
    "reflects the market valuation as of the date of the stock price."
)
ANSWER_40 = (
    "NVIDIA's most recent gross margin was 71.07%, while Apple's was 48.79%. The "
    "difference is 22.28 percentage points."
)


def case(id, expected, unit, tolerance):
    return Case(id=id, question="q", expected_answer=expected, unit=unit,
                tolerance=tolerance, answerable_via=("calculate_ratio",),
                company="NVDA", source="test")


CASE_38 = case("nvda-implied-pe-on-fy2026-eps", 45.71, "ratio (x)", 1.37)
CASE_39 = case("aapl-implied-pe-on-fy2025-eps", 41.97, "ratio (x)", 1.26)
CASE_40 = case("nvda-vs-aapl-gross-margin-gap", 22.28, "percentage points", 1.0)


def _score(c, answer, figure):
    return score_case(c, answer, figure, latency_ms=0, tool_calls=0, iterations=0,
                      prompt_tokens=0, completion_tokens=0, completed=True,
                      stop_reason="final_answer")


# ── the three run-b cases ─────────────────────────────────────────────────────

def test_case_38_scores_the_pe_it_stated_and_is_still_wrong():
    without = _score(CASE_38, ANSWER_38, None)
    assert without.extracted == 171.25 and without.extracted_from == FROM_ANSWER

    scored = _score(CASE_38, ANSWER_38, "21.65x")
    assert scored.extracted == 21.65 and scored.extracted_from == FROM_FINAL_FIGURE
    assert scored.verdict == "wrong", "a live TTM P/E is not the question's P/E"


def test_case_39_scores_the_pe_it_stated_and_is_still_wrong():
    without = _score(CASE_39, ANSWER_39, None)
    assert without.extracted == 231.29

    scored = _score(CASE_39, ANSWER_39, "39.05x")
    assert scored.extracted == 39.05 and scored.verdict == "wrong"


def test_case_40_scores_the_gap_it_stated_and_passes():
    without = _score(CASE_40, ANSWER_40, None)
    assert without.extracted == 71.07 and without.verdict == "wrong"

    scored = _score(CASE_40, ANSWER_40, "22.28 percentage points")
    assert scored.extracted == 22.28 and scored.verdict == "pass"


# ── the fallback and the edges ────────────────────────────────────────────────

def test_without_a_final_figure_the_score_is_exactly_the_prose_regex():
    for c, answer in ((CASE_38, ANSWER_38), (CASE_39, ANSWER_39), (CASE_40, ANSWER_40)):
        assert extract_scored(answer, None, c.unit)[0] == _score(c, answer, None).extracted


def test_a_final_figure_with_no_number_in_the_unit_falls_back_to_the_prose():
    # "21.65%" cannot be a ratio (x); the prose is scored instead.
    value, source = extract_scored(ANSWER_38, "21.65%", "ratio (x)")
    assert (value, source) == (171.25, FROM_ANSWER)


def test_a_declined_final_figure_is_no_number_not_a_stray_prose_figure():
    value, source = extract_scored(ANSWER_38, FINAL_FIGURE_NONE, "ratio (x)")
    assert (value, source) == (None, FROM_DECLINED)
    assert _score(CASE_38, ANSWER_38, "none").verdict == "no_number"


def test_scale_words_in_the_final_figure_convert_as_before():
    assert extract_scored("", "$215.9 billion", "USD millions") == (215900.0, FROM_FINAL_FIGURE)
    assert extract_scored("", "24,514 million shares", "millions of shares")[0] == 24514.0


def test_a_baseline_stored_before_final_figures_rescores_unchanged():
    row = {"case_id": CASE_40.id, "answer": ANSWER_40}           # no final_figure key
    [outcome] = rescore([row], {CASE_40.id: CASE_40})
    assert outcome.extracted == 71.07 and outcome.extracted_from == FROM_ANSWER

    row["final_figure"] = "22.28 percentage points"
    [outcome] = rescore([row], {CASE_40.id: CASE_40})
    assert outcome.extracted == 22.28 and outcome.verdict == "pass"


# ── splitting the line off the answer ─────────────────────────────────────────

@pytest.mark.parametrize("text,figure", [
    (ANSWER_40 + "\nFinal figure: 22.28 percentage points", "22.28 percentage points"),
    (ANSWER_40 + "\n\n**Final figure:** 22.28 percentage points.", "22.28 percentage points"),
    (ANSWER_40 + "\nfinal figure:  $1.2 billion  ", "$1.2 billion"),
    (ANSWER_40 + "\nFinal figure: none", FINAL_FIGURE_NONE),
    (ANSWER_40 + "\nFinal figure: N/A", FINAL_FIGURE_NONE),
])
def test_the_line_is_read_and_removed(text, figure):
    answer, got = split_final_figure(text)
    assert got == figure
    assert answer == ANSWER_40, "the line is not shown to the user"


def test_the_last_line_wins_and_every_line_is_removed():
    answer, got = split_final_figure(
        "Revenue was $130.5 billion.\nFinal figure: $130.5 billion\n"
        "Correction: it was $215.9 billion.\nFinal figure: $215.9 billion")
    assert got == "$215.9 billion"
    assert "Final figure" not in answer


def test_an_answer_without_the_line_is_untouched():
    assert split_final_figure(ANSWER_40) == (ANSWER_40, None)


# ── through the loop ──────────────────────────────────────────────────────────

def _drive(contents: list[str]):
    """One tool call returning a 71.07% margin, then the scripted answers."""
    reg = Registry()

    def get_margin(ticker: str) -> dict:
        """Get a margin.

        Args:
            ticker: the symbol.
        """
        return ok(ticker=ticker, value=0.710681, formatted="71.07%")

    reg.register(get_margin)
    script = [{"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "get_margin", "arguments": json.dumps({"ticker": "NVDA"})}}]}}]}]
    script += [{"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": c}}]} for c in contents]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=script.pop(0))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     base_url="http://model.invalid/v1") as client:
            return await run_agent("NVIDIA's gross margin?", registry=reg, client=client,
                                   model="fake-model", router_model="")
    return asyncio.run(go())


def test_the_loop_returns_the_figure_and_strips_it_from_the_answer():
    result = _drive(["NVIDIA's gross margin was 71.07%.\nFinal figure: 71.07%"])
    assert result.final_figure == "71.07%"
    assert result.answer == "NVIDIA's gross margin was 71.07%."
    assert result.to_dict()["final_figure"] == "71.07%"
    assert result.steps[-1].result == result.answer


def test_an_invented_final_figure_is_caught_by_the_grounding_guard():
    """The line is checked with the rest of the text, so a figure that appears
    only there, and traces to nothing, still triggers the guard."""
    invented = "NVIDIA's gross margin was 71.07%.\nFinal figure: 64.2%"
    result = _drive([invented, invented])
    assert result.stop_reason == "unverified_figures"
    assert result.answer.startswith("[Unverified:") and "64.2%" in result.answer.split("]")[0]
    assert result.final_figure == "64.2%", "recorded as written, for the scorer to judge"


def test_a_redirected_advisory_answer_carries_no_final_figure():
    result = _drive(["NVIDIA at 71.07% is a strong buy.\nFinal figure: 71.07%"])
    assert result.stop_reason == "investment_advice"
    assert result.final_figure is None


# -- an answer that is only the line -------------------------------------------

@pytest.mark.parametrize("only_the_line", [
    "Final figure: 71.07%", "**Final figure:** 71.07%.", "\n\nFinal figure: 71.07%\n",
])
def test_an_answer_that_is_only_the_line_still_reads_as_a_sentence(only_the_line):
    """It used to come back as "The model returned an empty response. This is a
    failure, not an answer." with the figure still recorded and scored."""
    result = _drive([only_the_line])
    assert result.answer == "The answer is 71.07%."
    assert result.final_figure == "71.07%"
    assert result.completed is True and result.stop_reason == "final_answer"
    assert result.steps[-1].result == result.answer and result.steps[-1].ok


def test_an_invented_figure_alone_on_the_line_is_still_flagged():
    """The sentence repeats the line's figure, so it changes nothing the guard
    decided: retried once, then answered under the caveat, never as fact."""
    result = _drive(["Final figure: 64.2%", "Final figure: 64.2%"])
    assert result.stop_reason == "unverified_figures" and result.completed is False
    assert result.answer.startswith("[Unverified:") and "64.2%" in result.answer.split("]")[0]
    # Marked where it stands as well (2026-11-18); the line itself, cut before
    # the user reads it, still carries the figure as written.
    assert result.answer.endswith("The answer is 64.2% [unverified].")
    assert result.final_figure == "64.2%"
    assert [s.tool for s in result.steps].count("ungrounded_answer") == 1


def test_a_line_that_declines_with_nothing_else_is_still_an_empty_response():
    result = _drive(["Final figure: none"])
    assert result.stop_reason == "empty_response" and result.completed is False
    assert result.final_figure == FINAL_FIGURE_NONE
    assert "empty response" in result.answer


def test_an_answer_with_prose_of_its_own_is_not_given_another_sentence():
    from agent.orchestrator import with_a_sentence
    text = "NVIDIA's gross margin was 71.07%.\nFinal figure: 71.07%"
    assert with_a_sentence(text) == text
    assert with_a_sentence("No line here.") == "No line here."
    assert with_a_sentence("") == ""
