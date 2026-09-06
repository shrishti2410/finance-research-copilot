"""Tests for the agent loop. The model is a scripted fake -- no inference server.

Each case drives the loop with a canned sequence of OpenAI-shaped responses, so
the behaviours under test are the loop's own: iteration accounting, tool-message
threading, and what happens when the budget runs out.
"""

import asyncio
import json

import httpx
import pytest

from agent.orchestrator import MAX_ITERATIONS, format_trace, run_agent
from tools.base import ok
from tools.registry import Registry


def run(coro):
    return asyncio.run(coro)


def tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def responds_with(message: dict) -> dict:
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


class FakeModel:
    """Replays scripted responses and records what it was sent."""

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        payload = self.script.pop(0) if self.script else responds_with({"content": "done"})
        return httpx.Response(200, json=payload)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), base_url="http://model.invalid/v1"
        )

    @property
    def last_messages(self) -> list[dict]:
        return self.requests[-1]["messages"]


@pytest.fixture
def registry():
    reg = Registry()

    def get_margin(ticker: str) -> dict:
        """Get a margin.

        Args:
            ticker: the symbol.
        """
        return ok(ticker=ticker, value=0.71)

    def broken(ticker: str) -> dict:
        """Always fails.

        Args:
            ticker: the symbol.
        """
        return {"ok": False, "error": "no_data", "message": "nothing for that ticker"}

    reg.register(get_margin)
    reg.register(broken)
    return reg


def drive(script: list[dict], registry, **kwargs):
    model = FakeModel(script)

    async def go():
        async with model.client() as client:
            return await run_agent(
                "compare margins", registry=registry, client=client,
                model="fake-model", **kwargs
            )

    return run(go()), model


# ── the straightforward paths ────────────────────────────────────────────────

def test_an_immediate_answer_takes_one_iteration(registry):
    # Deliberately figure-free. An immediate answer that states a number is a
    # different case entirely -- the grounding guard rejects it, and that has
    # its own tests below.
    result, _ = drive([responds_with({"content": "NVDA and AAPL are indexed."})], registry)

    assert result.answer == "NVDA and AAPL are indexed."
    assert result.completed is True
    assert result.stop_reason == "final_answer"
    assert result.iterations == 1
    assert result.tool_calls == []


def test_one_tool_call_then_an_answer(registry):
    result, _ = drive([
        responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "NVDA"})]}),
        responds_with({"content": "NVDA is at 71%."}),
    ], registry)

    assert result.iterations == 2
    assert [step.tool for step in result.steps] == ["get_margin", "final_answer"]
    assert result.steps[0].result["value"] == 0.71
    assert result.completed is True


def test_parallel_tool_calls_in_one_iteration(registry):
    """Two independent lookups should cost one turn, not two."""
    result, _ = drive([
        responds_with({"tool_calls": [
            tool_call("get_margin", {"ticker": "NVDA"}, "call_a"),
            tool_call("get_margin", {"ticker": "AAPL"}, "call_b"),
        ]}),
        responds_with({"content": "Both fetched."}),
    ], registry)

    assert result.iterations == 2
    assert len(result.tool_calls) == 2
    assert {step.iteration for step in result.tool_calls} == {1}


# ── message threading ────────────────────────────────────────────────────────

def test_the_system_prompt_and_question_open_the_conversation(registry):
    _, model = drive([responds_with({"content": "ok"})], registry)
    messages = model.last_messages

    assert messages[0]["role"] == "system"
    assert "financial research assistant" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "compare margins"}


def test_the_system_prompt_states_todays_date(registry):
    """Without it the model dates ranges from its training cutoff and gets
    empty results back."""
    from datetime import date

    _, model = drive([responds_with({"content": "ok"})], registry,
                     today=date(2026, 9, 5))
    assert "2026-09-05" in model.last_messages[0]["content"]


def test_history_is_replayed_before_the_new_question(registry):
    model = FakeModel([responds_with({"content": "ok"})])

    async def go():
        async with model.client() as client:
            return await run_agent(
                "and Apple?", history=[{"role": "user", "content": "NVDA margin?"},
                                       {"role": "assistant", "content": "71%"}],
                registry=registry, client=client,
            )

    run(go())
    roles = [m["role"] for m in model.last_messages]
    assert roles == ["system", "user", "assistant", "user"]


def test_the_assistant_tool_call_message_is_echoed_back(registry):
    """Tool results are matched to the request by tool_call_id. Dropping the
    assistant message that asked for them orphans every one."""
    _, model = drive([
        responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "NVDA"}, "call_z")]}),
        responds_with({"content": "done"}),
    ], registry)

    messages = model.last_messages
    assistant = next(m for m in messages if m["role"] == "assistant")
    tool_message = next(m for m in messages if m["role"] == "tool")

    assert assistant["tool_calls"][0]["id"] == "call_z"
    assert tool_message["tool_call_id"] == "call_z"
    assert tool_message["name"] == "get_margin"


def test_tool_results_reach_the_model_as_json(registry):
    _, model = drive([
        responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "NVDA"})]}),
        responds_with({"content": "done"}),
    ], registry)

    tool_message = next(m for m in model.last_messages if m["role"] == "tool")
    assert json.loads(tool_message["content"])["value"] == 0.71


def test_the_tool_schemas_are_sent_every_iteration(registry):
    _, model = drive([
        responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "NVDA"})]}),
        responds_with({"content": "done"}),
    ], registry)

    for request in model.requests:
        assert {t["function"]["name"] for t in request["tools"]} == {"get_margin", "broken"}


def test_temperature_defaults_to_zero(registry):
    """Tool selection should not be a dice roll."""
    _, model = drive([responds_with({"content": "ok"})], registry)
    assert model.requests[0]["temperature"] == 0.0


# ── failure inside the loop ──────────────────────────────────────────────────

def test_a_failing_tool_does_not_stop_the_loop(registry):
    """A tool failure is data the model can act on, not a reason to abort."""
    result, model = drive([
        responds_with({"tool_calls": [tool_call("broken", {"ticker": "ZZZZ"})]}),
        responds_with({"content": "That ticker has no data."}),
    ], registry)

    assert result.completed is True
    assert result.steps[0].ok is False
    assert "nothing for that ticker" in json.loads(
        next(m for m in model.last_messages if m["role"] == "tool")["content"]
    )["message"]


def test_an_unknown_tool_is_reported_back_to_the_model(registry):
    result, _ = drive([
        responds_with({"tool_calls": [tool_call("get_weather", {})]}),
        responds_with({"content": "No such tool."}),
    ], registry)

    assert result.steps[0].ok is False
    assert result.steps[0].result["error"] == "unsupported"


def test_malformed_tool_arguments_are_handled_as_a_tool_failure(registry):
    """Arguments arrive as a JSON string a model can get wrong. That is a bad
    call to report back, not an exception."""
    result, _ = drive([
        responds_with({"tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_margin", "arguments": "{ticker: NVDA"},
        }]}),
        responds_with({"content": "recovered"}),
    ], registry)

    assert result.steps[0].ok is False
    assert "not valid JSON" in result.steps[0].result["message"]
    assert result.completed is True


def test_an_unreachable_model_is_reported_as_infrastructure_not_an_answer(registry):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(refuse), base_url="http://model.invalid/v1"
        ) as client:
            return await run_agent("q", registry=registry, client=client)

    result = run(go())
    assert result.completed is False
    assert result.stop_reason == "inference_error"
    assert "infrastructure failure" in result.answer


def test_an_empty_model_response_is_not_passed_off_as_an_answer(registry):
    result, _ = drive([responds_with({"content": "   "})], registry)

    assert result.completed is False
    assert result.stop_reason == "empty_response"
    assert result.answer  # never the empty string


# ── the iteration budget ─────────────────────────────────────────────────────

def test_the_loop_stops_at_the_budget(registry):
    """A model that keeps calling tools must not run forever."""
    forever = [responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "NVDA"})]})] * 20
    result, model = drive(forever, registry, max_iterations=3)

    assert result.iterations == 3
    assert len(model.requests) == 3
    assert result.completed is False
    assert result.stop_reason == "max_iterations"


def test_exhaustion_says_so_and_reports_what_it_found(registry):
    """Not an empty string and not a truncated draft: the caller must be able to
    tell a real answer from a loop that ran out."""
    forever = [responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "NVDA"})]})] * 20
    result, _ = drive(forever, registry, max_iterations=3)

    assert "couldn't complete" in result.answer
    assert "3-step limit" in result.answer
    assert "get_margin" in result.answer          # the findings survived
    assert result.steps[-1].tool == "final_answer"


def test_exhaustion_with_nothing_useful_says_that_too(registry):
    """Listing failed calls as 'findings' would read like partial evidence."""
    forever = [responds_with({"tool_calls": [tool_call("broken", {"ticker": "Z"})]})] * 20
    result, _ = drive(forever, registry, max_iterations=2)

    assert "no findings to report" in result.answer


def test_the_default_budget_is_five():
    assert MAX_ITERATIONS == 5


# ── the trace ────────────────────────────────────────────────────────────────

def test_every_step_records_the_required_columns(registry):
    result, _ = drive([
        responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "NVDA"})]}),
        responds_with({"content": "done"}),
    ], registry)

    for step in result.steps:
        row = step.to_dict()
        assert set(row) >= {"iteration", "tool", "arguments", "result", "latency_ms"}
        assert isinstance(row["iteration"], int)
        assert row["latency_ms"] >= 0


def test_the_final_answer_is_its_own_row(registry):
    result, _ = drive([responds_with({"content": "Both are indexed."})], registry)
    assert result.steps[-1].tool == "final_answer"
    assert result.steps[-1].result == "Both are indexed."


def test_the_table_renders_one_row_per_step(registry):
    result, _ = drive([
        responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "NVDA"})]}),
        responds_with({"content": "done"}),
    ], registry)

    lines = result.table().splitlines()
    assert lines[0].split() == ["#", "tool", "arguments", "result", "latency", "model"]
    assert len(lines) == 2 + len(result.steps)   # header + rule + rows
    assert "get_margin" in lines[2]


def test_the_table_truncates_rather_than_wrapping(registry):
    """A 25-article news result would otherwise be one unreadable row."""
    result, _ = drive([
        responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "NVDA"})]}),
        responds_with({"content": "x" * 5000}),
    ], registry)

    assert all(len(line) < 400 for line in result.table().splitlines())


def test_an_empty_trace_still_renders(registry):
    assert "tool" in format_trace([])


def test_result_serializes_for_storage(registry):
    """The trace is written to a JSONB column on the assistant message."""
    result, _ = drive([
        responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "NVDA"})]}),
        responds_with({"content": "done"}),
    ], registry)

    assert json.loads(json.dumps(result.to_dict(), default=str))["iterations"] == 2


# ── the ungrounded-answer guard ──────────────────────────────────────────────
#
# Conversation history made the model answer follow-ups from recall instead of
# calling a tool. `tool_choice: "required"` is accepted and ignored by Ollama,
# so the loop catches it after the fact and gives the model one correction.

def test_a_figure_with_no_tool_call_is_rejected_and_retried(registry):
    result, model = drive([
        responds_with({"content": "Apple's gross margin was 38.03%."}),
        responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "AAPL"})]}),
        responds_with({"content": "Apple's gross margin is 71%, per the tool."}),
    ], registry, history=[
        {"role": "user", "content": "What is NVIDIA's gross margin?"},
        {"role": "assistant", "content": "71.07%."},
    ])

    assert [s.tool for s in result.steps] == [
        "ungrounded_answer", "get_margin", "final_answer",
    ]
    # The discarded answer is on the trace, not silently dropped: it is the
    # only evidence of why the run took an extra iteration.
    assert result.steps[0].ok is False
    assert result.steps[0].result == "Apple's gross margin was 38.03%."
    assert result.completed is True
    assert "38.03" not in result.answer


def test_the_correction_reaches_the_model_as_a_user_message(registry):
    """Measured, not assumed: the same text as a system message left the model
    answering from recall again; as a user message it called the tool."""
    from agent.orchestrator import UNGROUNDED_RETRY

    _, model = drive([
        responds_with({"content": "It was 38.03%."}),
        responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "AAPL"})]}),
        responds_with({"content": "done"}),
    ], registry)

    correction = model.requests[1]["messages"][-1]
    assert correction["role"] == "user"
    assert correction["content"] == UNGROUNDED_RETRY
    # The rejected answer stays in front of it, so the correction has a referent.
    assert model.requests[1]["messages"][-2] == {
        "role": "assistant", "content": "It was 38.03%."
    }


def test_an_answer_backed_by_a_successful_tool_call_is_kept(registry):
    """The guard must not fire on a grounded figure -- that is the normal path."""
    result, _ = drive([
        responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "NVDA"})]}),
        responds_with({"content": "NVIDIA's gross margin is 71.0%."}),
    ], registry)

    assert [s.tool for s in result.steps] == ["get_margin", "final_answer"]
    assert result.answer == "NVIDIA's gross margin is 71.0%."


def test_an_answer_with_no_figure_is_left_alone(registry):
    """"What can you do?" is not a claim about a value."""
    result, _ = drive([
        responds_with({"content": "I can look up filings, prices, ratios and news."}),
    ], registry)

    assert [s.tool for s in result.steps] == ["final_answer"]
    assert result.iterations == 1


def test_the_guard_fires_at_most_once(registry):
    """A model that ignores the correction gets an answer out, not a loop."""
    result, model = drive([
        responds_with({"content": "38.03%."}),
        responds_with({"content": "Still 38.03%."}),
    ], registry)

    assert [s.tool for s in result.steps] == ["ungrounded_answer", "final_answer"]
    assert result.answer == "Still 38.03%."
    assert result.iterations == 2
    assert len(model.requests) == 2


def test_a_failed_tool_call_does_not_count_as_grounding(registry):
    """The tool returned ok:false, so the figure still came from nowhere."""
    result, _ = drive([
        responds_with({"tool_calls": [tool_call("broken", {"ticker": "ZZZZ"})]}),
        responds_with({"content": "It was 38.03% anyway."}),
        responds_with({"tool_calls": [tool_call("get_margin", {"ticker": "AAPL"})]}),
        responds_with({"content": "71%, from the tool."}),
    ], registry)

    assert [s.tool for s in result.steps] == [
        "broken", "ungrounded_answer", "get_margin", "final_answer",
    ]


@pytest.mark.parametrize("answer,is_figure", [
    ("Gross margin was 46.9%.", True),
    ("Revenue was $416,161 million.", True),
    ("Revenue was 416.2 billion.", True),
    ("The ratio is 1.85.", True),
    ("Revenue was 416,161.", True),
    ("Its fiscal year 2026 ended in January.", False),
    ("I could not find that filing.", False),
    ("Both NVDA and AAPL are indexed.", False),
])
def test_what_counts_as_stating_a_figure(answer, is_figure):
    from agent.orchestrator import states_a_figure
    assert states_a_figure(answer) is is_figure
