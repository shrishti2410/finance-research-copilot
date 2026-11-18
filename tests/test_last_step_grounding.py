"""An answer written on the last step that states a figure no tool returned.

The last step has no tools and no step after it, so the grounding retry
cannot help. Until this change it was tried anyway: the loop ran out, the
answer was dropped, and the user got the step-limit message with the facts as
a list -- safe, but a readable answer was thrown away for one figure.

Now the answer is kept and flagged twice: the [Unverified: ...] banner names
the figures, and each one is marked where it stands. The rule this file pins
(approved 2026-11-18): an unsupported figure never reaches the user
unflagged. The inline mark applies on every [Unverified] path, not only the
last step.
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from agent import orchestrator
from agent.grounding import unsupported_figures
from agent.orchestrator import Step, mark_unverified, run_agent
from tools.registry import Registry

pytestmark = pytest.mark.safety

GOOD = "NVIDIA's fiscal 2026 revenue was $215.938 billion."
BAD = "NVIDIA's fiscal 2026 revenue was $215.938 billion, and its gross margin was 81.3%."
FACT = {"ok": True, "ticker": "NVDA", "fact": "revenue", "fiscal_year": 2026,
        "description": "Total revenue (net sales)", "value": 215938000000, "unit": "USD",
        "formatted": "$215.938 billion", "accession": "0001045810-26-000021"}


def _registry():
    def get_financial_fact(ticker: str, fact: str, fiscal_year: str) -> dict:
        """Get one reported financial figure.

        Args:
            ticker: A ticker.
            fact: A fact name.
            fiscal_year: A fiscal year.
        """
        return dict(FACT)
    reg = Registry()
    reg.register(get_financial_fact, name="get_financial_fact")
    return reg


class Model:
    """Calls the tool whenever tools are offered, so it reaches the last step,
    then writes `answers` in turn, the last one repeated. With `eager`, it
    calls the tool once and answers from the second step on."""

    def __init__(self, *answers, eager=False):
        self.answers = list(answers)
        self.eager = eager
        self.requests = []

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        n = len(self.requests)
        if body.get("tools") and not (self.eager and n > 1):
            msg = {"content": "", "tool_calls": [{"id": f"c{n}", "type": "function", "function": {
                "name": "get_financial_fact",
                "arguments": json.dumps({"ticker": "NVDA", "fact": "revenue", "fiscal_year": "latest"})}}]}
        else:
            text = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            msg = {"content": text + "\nFinal figure: none"}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})


def _run(model):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(model.handler),
                                     base_url="http://model.invalid/v1") as client:
            return await run_agent("What is your analysis of NVIDIA?", registry=_registry(),
                                   client=client, model="fake-model", router_model="")
    return asyncio.run(go())


# --- the last step ---------------------------------------------------------------------

def test_a_last_step_answer_with_an_unsupported_figure_is_kept_and_flagged():
    model = Model(BAD)
    result = _run(model)
    assert len(model.requests) == orchestrator.MAX_ITERATIONS      # no call after the last
    assert result.stop_reason == "unverified_figures"              # not "max_iterations"
    assert not result.completed
    assert result.answer.startswith("[Unverified: I could not trace 81.3%")
    assert "81.3% [unverified]" in result.answer
    assert "billion [unverified]" not in result.answer             # the grounded one is not marked
    assert "step limit" not in result.answer.lower()


def test_a_grounded_last_step_answer_is_unchanged():
    result = _run(Model(GOOD))
    assert result.completed and result.stop_reason == "final_answer"
    assert "[unverified" not in result.answer.lower()
    assert result.answer.startswith(GOOD)


def test_the_last_step_is_not_followed_by_a_retry_request():
    model = Model(BAD)
    _run(model)
    # The only user messages the model saw are the question and LAST_TURN.
    users = [m["content"] for m in model.requests[-1]["messages"] if m["role"] == "user"]
    assert users[-1] == orchestrator.LAST_TURN


# --- every [Unverified] path marks the figure where it stands ----------------------------

def test_an_answer_still_unsupported_after_the_retry_is_marked_inline():
    model = Model(BAD, BAD, eager=True)
    result = _run(model)
    assert len(model.requests) == 3                                # tool, answer, retried answer
    assert result.stop_reason == "unverified_figures"
    assert result.answer.startswith("[Unverified:")
    assert "81.3% [unverified]" in result.answer


def test_a_retry_that_fixes_the_figure_leaves_no_mark():
    result = _run(Model(BAD, GOOD, eager=True))
    assert result.completed
    assert "[unverified" not in result.answer.lower()


# --- mark_unverified ---------------------------------------------------------------------

def _figures(answer):
    step = Step(iteration=1, tool="get_financial_fact", arguments={}, result=dict(FACT),
                latency_ms=1.0, model_latency_ms=1.0, ok=True)
    return unsupported_figures(answer, [step])


def test_each_figure_is_marked_at_its_own_place():
    answer = "Margin was 81.3%, EPS was $7.12, and margin was 81.3% again."
    figures = _figures(answer)
    assert {f.text for f in figures} == {"81.3%", "$7.12"}        # each distinct figure once
    assert mark_unverified(answer, figures) == (
        "Margin was 81.3% [unverified], EPS was $7.12 [unverified], "
        "and margin was 81.3% [unverified] again.")


def test_marking_with_no_figures_changes_nothing():
    assert mark_unverified(GOOD, []) == GOOD


@pytest.mark.parametrize("answer", [
    BAD,
    "Revenue grew 12.5% to $215.938 billion; net margin was 55.6%.",
    "Revenue was $215.938 billion (up from $130.5 billion), EPS $4.90.",
])
def test_no_unsupported_figure_is_left_unmarked(answer):
    figures = _figures(answer)
    assert figures
    marked = mark_unverified(answer, figures)
    for f in figures:
        assert re.search(re.escape(f.text) + r" \[unverified\]", marked), (f.text, marked)


def test_the_final_figure_line_is_not_marked_so_the_grader_reads_it_as_before():
    # The line is cut from the text before the user sees it (split_final_figure);
    # its value goes to the eval. A mark there would change what the grader reads.
    answer = "Gross margin was 81.3%.\nFinal figure: 81.3%"
    marked = mark_unverified(answer, _figures(answer))
    assert marked == "Gross margin was 81.3% [unverified].\nFinal figure: 81.3%"


def test_a_figure_only_answer_has_its_sentence_marked():
    from agent.orchestrator import split_final_figure, with_a_sentence
    text = with_a_sentence("Final figure: 81.3%")
    marked = mark_unverified(text, _figures(text))
    shown, figure = split_final_figure(marked)
    assert "81.3% [unverified]" in shown and figure == "81.3%"
