"""The agent loop: ask the model, run the tools it asks for, repeat, answer.

    result = await run_agent("Compare NVIDIA's gross margin to Apple's")
    print(result.answer)
    print(result.table())

One iteration is one model call plus every tool call it requested. The loop ends
when the model replies without asking for a tool, or when the iteration budget
runs out -- and those two endings are distinguishable by the caller, which is
the point of `completed` and `stop_reason`.

Running out of turns is not an empty answer
-------------------------------------------
A loop that hits its limit and returns "" or a half-written draft is worse than
one that fails: the caller cannot tell a real answer from a truncated one, and
the user gets something that reads like a conclusion. So exhaustion returns a
written message that says it did not finish and lists what the tools did
establish -- see `agent/prompts.py`.

Native tool calling, not parsed text
------------------------------------
Qwen is served through Ollama's OpenAI-compatible endpoint, which emits real
`tool_calls` with a `finish_reason` of "tool_calls". Nothing here regex-scrapes
JSON out of prose. Malformed arguments are still possible -- they arrive as a
JSON string that may not parse -- and that is handled as a tool failure the
model can read and retry, not as an exception.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from agent.prompts import (
    EXHAUSTED_NO_FINDINGS,
    EXHAUSTED_TEMPLATE,
    EXHAUSTED_TOOL_BUDGET,
    EXHAUSTED_TOOL_BUDGET_NO_FINDINGS,
    system_prompt,
)
from agent.grounding import unsupported_figures
from agent.tools import build_registry, compact_for_model
from core.config import settings
from tools.registry import Registry

log = logging.getLogger(__name__)

MAX_ITERATIONS = 5

# ─────────────────────────────────────────────────────────────────────────────
# The per-iteration tool-call budget
# ─────────────────────────────────────────────────────────────────────────────
#
# `max_iterations` bounds model round-trips, not work. Qwen issues parallel tool
# calls, and one iteration holds as many as it decides to emit -- the audit's
# fan-out query ("for NVDA, AAPL and MSFT: gross margin, debt-to-equity and P/E
# each, then news, then prices, then what the 10-K says") produced 16 calls
# inside a single iteration, all executed, against a cap that had counted one.
# The cap was doing nothing about the cost that actually matters: yfinance and
# RSS round trips, and the tool output that has to fit back in the context.
#
# 8 is the default because it is above what a real comparison needs and below a
# runaway. Two companies across the four tools is at most 8; the 16-call query
# is three companies times five things each, which is a research plan, not a
# turn. A model that wants more can have it in the next iteration -- the budget
# is per iteration, and iterations are what `max_iterations` is for.
MAX_TOOL_CALLS_PER_ITERATION = 8

# How much of a result is rendered into the trace table. The full value is kept
# on the step and stored with the message; this is only what fits on a screen.
TABLE_RESULT_CHARS = 88

# ─────────────────────────────────────────────────────────────────────────────
# The ungrounded-answer guard
# ─────────────────────────────────────────────────────────────────────────────
#
# Conversation history introduced a failure that single-turn runs never had.
# Asked "What is NVIDIA's gross margin?" the model calls calculate_ratio. Asked
# "What about Apple's?" with the previous exchange in front of it, it resolves
# the reference correctly and then answers from recall -- 38.03% for a figure
# that is actually 46.91%, with no tool call at all. Having just seen a question
# of that shape answered, it imitates the answer instead of the method.
#
# Three fixes were tried, in order:
#   1. A prompt rule saying history supplies the subject and never the figures.
#      Measured: the model still answered from recall. Kept anyway -- it is
#      correct guidance and costs nothing -- but it is not the mechanism.
#   2. `tool_choice: "required"`. Ollama accepts the field and ignores it:
#      finish_reason came back "stop" with an empty tool_calls array, same as
#      "auto". Not available here.
#   3. Catching it after the fact, below. A user-role correction moved the model
#      to calculate_ratio on the retry; the same text as a system message did
#      not, which is why the correction is addressed to it as the user.
#
# The check is narrow on purpose. It fires only when the run made no successful
# tool call *and* the answer states a figure, so "hello" and "what can you do?"
# are unaffected, and one retry is allowed per run -- a model that ignores the
# correction produces an answer, not an infinite loop.
#
# It costs one redundant call on a pure comparison follow-up ("which one is
# higher?"), because a figure established in an earlier turn is not a figure a
# tool returned in this one. That trade, and what would justify revisiting it,
# is written up in docs/KNOWN_LIMITATIONS.md.

MAX_GROUNDING_RETRIES = 1

# What counts as stating a figure: a percentage, an amount of money, a scaled
# quantity, a decimal, or a thousands-separated integer. Deliberately not "any
# digit" -- "fiscal 2026" and "the last 30 days" are not claims about a value.
_FIGURE = re.compile(
    r"\d+(?:\.\d+)?\s*%"
    r"|[$€£¥]\s*\d"
    r"|\d+(?:\.\d+)?\s*(?:billion|million|trillion|bn\b|tn\b)"
    r"|\d+\.\d+"
    r"|\d{1,3}(?:,\d{3})+",
    re.I,
)

# Addressed to the model as the user, because that is what was measured to work.
UNGROUNDED_RETRY = (
    "These figures in your answer do not appear in any tool result from this "
    "turn: {figures}. They are from memory or from an assumption, not from "
    "data, so they cannot be used -- discard them. Either call a tool that "
    "produces them and answer from what it returns, or say plainly that you "
    "could not establish them. Do not assume a value in order to compute one."
)

# Used when the retry is spent and figures are still unaccounted for. The
# answer is prefixed with this rather than replaced: its prose is usually
# right, and the caller is owed what was established. What it must never do is
# arrive looking like a reported figure.
UNVERIFIED_CAVEAT = (
    "[Unverified: I could not trace {figures} to any tool result in this "
    "turn, so treat those numbers as unconfirmed rather than as reported "
    "figures.]\n\n"
)


def states_a_figure(answer: str) -> bool:
    """Whether an answer asserts a numeric value a tool should have produced."""
    return bool(_FIGURE.search(answer))


# ─────────────────────────────────────────────────────────────────────────────
# Streaming the model call
# ─────────────────────────────────────────────────────────────────────────────
#
# The loop can take either path per iteration. Buffered is the default and is
# what every existing caller and test uses. Streaming exists so a UI can show an
# answer as it is written instead of after 40s of nothing -- and, just as
# importantly, so it can show *which tool is running* while it runs.
#
# Both paths return the same assembled assistant message, so everything
# downstream -- the grounding guard, the tool-call budget, the trace -- is
# identical either way. The only difference is that the streaming path calls
# `on_token` as content arrives.


def _merge_tool_call_delta(acc: dict[int, dict], delta: dict) -> None:
    """Fold one streamed tool_call fragment into the accumulator.

    Tool calls arrive split across chunks and are keyed by `index`, not by id:
    the id and function name typically come in the first fragment for that
    index and the arguments dribble in over many more. Appending by index is
    the only correct way to reassemble them -- concatenating in arrival order
    interleaves two parallel calls into one unparseable blob.
    """
    index = delta.get("index", 0)
    slot = acc.setdefault(
        index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
    )
    if delta.get("id"):
        slot["id"] = delta["id"]
    function = delta.get("function") or {}
    if function.get("name"):
        slot["function"]["name"] = function["name"]
    if function.get("arguments"):
        slot["function"]["arguments"] += function["arguments"]


async def _stream_message(client, payload, on_token) -> dict:
    """Run one streaming chat completion and return the assembled message.

    Returns the same shape the buffered path pulls out of `choices[0].message`.
    """
    content_parts: list[str] = []
    tool_calls: dict[int, dict] = {}

    request = client.build_request("POST", "/chat/completions",
                                   json={**payload, "stream": True})
    response = await client.send(request, stream=True)
    try:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                # A malformed chunk is not worth aborting a good answer over.
                log.warning("agent: skipping unparseable stream chunk: %s", data[:120])
                continue

            delta = ((chunk.get("choices") or [{}])[0]).get("delta") or {}
            piece = delta.get("content")
            if piece:
                content_parts.append(piece)
                # Swallowed for the same reason _emit swallows: a consumer that
                # has gone away is not an inference failure, and the tool
                # results already gathered are still worth storing. Without
                # this, a closed browser tab surfaces to the user as "I
                # couldn't reach the model".
                if on_token is not None:
                    try:
                        await on_token(piece)
                    except Exception:  # noqa: BLE001
                        log.debug("agent: token consumer raised, continuing",
                                  exc_info=True)
            for call_delta in delta.get("tool_calls") or []:
                _merge_tool_call_delta(tool_calls, call_delta)
    finally:
        await response.aclose()

    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return message


async def _router_message(client, payload) -> dict | None:
    """Run the router's call, abandoning it the moment it starts writing prose.

    Returns the assembled tool-call message, or None if the router had no tool
    call to make -- which is the signal to hand the turn to the larger model.

    Why this is streamed when the router's output is never shown: letting the
    router finish a draft answer costs about what routing saves. Measured on
    this host, the routing step drops from 13.7s to 3.7s, and a 128-token draft
    at 13.9 tok/s is 9s of that back. Aborting at the first content token costs
    time-to-first-token instead, about 1.5s.

    That works because of how Ollama emits the two shapes: a tool call arrives as
    a single `tool_calls` delta with no content deltas at all, while prose
    arrives as a stream of content deltas. Measured over five questions -- three
    that call a tool, two that cannot -- the tool-calling ones emitted zero
    content deltas and the prose ones emitted their first at ~1.5s. So a
    non-empty content delta is an unambiguous "this is not a tool call".
    """
    tool_calls: dict[int, dict] = {}
    request = client.build_request("POST", "/chat/completions",
                                   json={**payload, "stream": True})
    response = await client.send(request, stream=True)
    prose = False
    try:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                log.warning("agent: skipping unparseable router chunk: %s", data[:120])
                continue
            delta = ((chunk.get("choices") or [{}])[0]).get("delta") or {}
            for call_delta in delta.get("tool_calls") or []:
                _merge_tool_call_delta(tool_calls, call_delta)
            if delta.get("content"):
                # Stop reading. The rest of this draft is generated either way --
                # nothing here can cancel work already queued upstream -- but it
                # is not waited for, which is the part that costs.
                prose = True
                break
    finally:
        await response.aclose()

    if prose or not tool_calls:
        return None
    return {"role": "assistant", "content": "",
            "tool_calls": [tool_calls[i] for i in sorted(tool_calls)]}


@dataclass
class Step:
    """One row of the trace: a tool call, or the final answer."""

    iteration: int
    tool: str                       # tool name, or "final_answer"
    arguments: dict[str, Any]
    result: Any
    latency_ms: float               # tool execution, or model time for the answer
    model_latency_ms: float         # the model call that produced this step
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "tool": self.tool,
            "arguments": self.arguments,
            "result": self.result,
            "latency_ms": round(self.latency_ms, 1),
            "model_latency_ms": round(self.model_latency_ms, 1),
            "ok": self.ok,
        }


@dataclass
class AgentResult:
    answer: str
    steps: list[Step] = field(default_factory=list)
    iterations: int = 0
    completed: bool = True          # False when the budget ran out
    stop_reason: str = "final_answer"
    model: str = ""
    # Empty when the whole turn ran on `model`. See settings.agent_router_model.
    router_model: str = ""
    total_ms: float = 0.0
    # Summed across every model call in the run. Zero when the upstream did not
    # report usage -- which the streaming path does not -- so a zero here means
    # "not measured", not "free". `usage_measured` says which.
    #
    # These cover calls to `model`. The router's calls are streamed and so
    # report no usage: when `router_calls` is above zero the totals are the
    # larger model's share, not the whole run's.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_measured: bool = False
    router_calls: int = 0

    @property
    def tool_calls(self) -> list[Step]:
        return [step for step in self.steps if step.tool != "final_answer"]

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "iterations": self.iterations,
            "completed": self.completed,
            "stop_reason": self.stop_reason,
            "model": self.model,
            "router_model": self.router_model,
            "router_calls": self.router_calls,
            "total_ms": round(self.total_ms, 1),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "usage_measured": self.usage_measured,
            "steps": [step.to_dict() for step in self.steps],
        }

    def table(self, width: int = TABLE_RESULT_CHARS) -> str:
        """The trace as a table: iteration, tool, arguments, result, latency."""
        return format_trace(self.steps, width)


def _one_line(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_trace(steps: list[Step], width: int = TABLE_RESULT_CHARS) -> str:
    headers = ("#", "tool", "arguments", "result", "latency", "model")
    rows = [
        (
            str(step.iteration),
            step.tool if step.ok else f"{step.tool} ✗",
            _one_line(step.arguments, 46),
            _one_line(step.result, width),
            f"{step.latency_ms:,.0f}ms",
            f"{step.model_latency_ms:,.0f}ms",
        )
        for step in steps
    ]

    widths = [max(len(header), *(len(row[i]) for row in rows)) if rows else len(header)
              for i, header in enumerate(headers)]
    line = "─".join("─" * w for w in widths)

    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)), line]
    out += ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)) for row in rows]
    return "\n".join(out)


def _parse_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Tool arguments arrive as a JSON *string*. A model can produce invalid JSON."""
    if isinstance(raw, dict):
        return raw, None
    if raw in (None, ""):
        return {}, None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return {}, f"arguments were not valid JSON ({exc}): {raw!r}"
    if not isinstance(parsed, dict):
        return {}, f"arguments must be a JSON object, got {type(parsed).__name__}"
    return parsed, None


async def _emit(on_event, event: dict[str, Any]) -> None:
    """Hand one event to the caller's channel, if there is one.

    Never lets a consumer's failure take down the run: a browser closing its
    connection mid-answer must not turn into a failed agent call, because the
    tool results are still worth storing.
    """
    if on_event is None:
        return
    try:
        await on_event(event)
    except Exception:  # noqa: BLE001 - a dead consumer is not a failed run
        log.debug("agent: event consumer raised, continuing", exc_info=True)


def _findings(steps: list[Step]) -> str:
    """What the successful tool calls established, for the exhaustion message."""
    lines = [
        f"- {step.tool}({_one_line(step.arguments, 60)}): {_one_line(step.result, 220)}"
        for step in steps if step.ok and step.tool != "final_answer"
    ]
    return "\n".join(lines)


async def run_agent(
    question: str,
    history: list[dict[str, str]] | None = None,
    *,
    registry: Registry | None = None,
    model: str | None = None,
    router_model: str | None = None,
    max_tokens: int | None = None,
    router_max_tokens: int | None = None,
    max_iterations: int = MAX_ITERATIONS,
    max_tool_calls_per_iteration: int = MAX_TOOL_CALLS_PER_ITERATION,
    client: httpx.AsyncClient | None = None,
    today: date | None = None,
    temperature: float = 0.0,
    stream_tokens: bool = False,
    on_token: Callable[[str], Awaitable[None]] | None = None,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> AgentResult:
    """Answer `question`, calling tools as needed, within `max_iterations` turns.

    Args:
        question: the user's message.
        history: prior turns as [{"role", "content"}], oldest first.
        registry: tools to expose. Defaults to the four in `agent.tools`.
        max_iterations: model round-trips. Bounds turns, not work.
        max_tool_calls_per_iteration: tool calls run within one iteration. A
            model that asks for more gets the first `max_tool_calls_per_iteration`
            executed and then the same honest stop as running out of
            iterations -- see the note on the constant.
        model: model id. Defaults to `settings.agent_model`.
        router_model: a smaller model for the tool-selection steps, with `model`
            reserved for writing the answer and for recovering from a failed
            tool call. Defaults to `settings.agent_router_model`; pass "" to run
            every step on `model`.
        max_tokens: cap on generated tokens per call to `model`. Unbounded
            generation on a CPU host is a minutes-long tail, not a longer
            answer -- see the note on `settings.agent_max_tokens`.
        router_max_tokens: the same cap for `router_model`.
        client: an httpx client to reuse. One is created and closed if omitted.
        today: overrides the date given to the model, for reproducible tests.
        temperature: 0 by default -- tool selection should not be a dice roll.
        stream_tokens: request streaming completions so `on_token` fires as the
            answer is written. Off by default; the assembled message, and so
            everything the loop decides from it, is identical either way.
        on_token: awaited with each content fragment. Fragments from a draft the
            grounding guard later discards are included -- a "discarded" event
            follows, and a consumer showing text must reset on it.
        on_event: awaited with progress events: iteration, tool_start,
            tool_result, discarded, budget. Exceptions from it are swallowed, so
            a consumer that goes away cannot fail the run.
    """
    if max_tool_calls_per_iteration < 1:
        # A budget of zero would let the model request tools it can never run,
        # and every question would end on the over-budget path. That is a
        # misconfiguration, not a policy, so it fails where it is set.
        raise ValueError(
            f"max_tool_calls_per_iteration must be at least 1, "
            f"got {max_tool_calls_per_iteration}"
        )

    registry = registry or build_registry()
    model = model or settings.agent_model
    # "" is a deliberate opt-out, so `is None` rather than falsiness.
    router_model = (settings.agent_router_model if router_model is None
                    else router_model)
    if router_model == model:
        # Nothing is gained by escalating a model to itself, and the escalation
        # would cost a second full call on the answer step.
        router_model = ""
    max_tokens = settings.agent_max_tokens if max_tokens is None else max_tokens
    router_max_tokens = (settings.agent_router_max_tokens
                         if router_max_tokens is None else router_max_tokens)
    started = time.perf_counter()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt(max_iterations, today)}
    ]
    messages += list(history or [])
    messages.append({"role": "user", "content": question})

    steps: list[Step] = []
    grounding_retries = 0
    escalate_next = False
    router_calls = 0
    prompt_tokens = completion_tokens = 0
    usage_measured = False
    owns_client = client is None
    client = client or httpx.AsyncClient(
        base_url=settings.agent_inference_base_url,
        timeout=httpx.Timeout(settings.inference_read_timeout,
                              connect=settings.inference_connect_timeout),
        # The agent calls the app's own /v1 proxy. Without this the loop would
        # spend the caller's rate-limit budget on itself -- five inference calls
        # per question against an anonymous IP limit of twenty a minute.
        headers={"X-Internal-Token": settings.internal_token},
    )

    async def one_call(call_model: str, cap: int, stream: bool) -> dict:
        """One chat completion. Accumulates usage; raises on transport failure."""
        nonlocal prompt_tokens, completion_tokens, usage_measured
        payload = {
            "model": call_model,
            "messages": messages,
            "tools": registry.schemas(),
            "temperature": temperature,
            "max_tokens": cap,
            "stream": False,
        }
        if stream:
            return await _stream_message(client, payload, on_token)
        response = await client.post("/chat/completions", json=payload)
        response.raise_for_status()
        body = response.json()
        # Ollama reports usage on the buffered path only. Summed across
        # iterations, because one question is several calls and the per-call
        # number is not what anything wants.
        usage = body.get("usage") or {}
        if usage:
            usage_measured = True
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("completion_tokens") or 0)
        return ((body.get("choices") or [{}])[0]).get("message") or {}

    try:
        for iteration in range(1, max_iterations + 1):
            # The router handles tool selection; agent_model writes the answer.
            # Two things take routing away from it. A tool call that failed last
            # iteration needs recovery, which the small model measurably cannot
            # do -- see settings.agent_router_model. And a grounding retry is an
            # instruction to go and verify a figure, which is the same kind of
            # work.
            use_router = bool(router_model) and not escalate_next
            escalate_next = False

            call_started = time.perf_counter()
            await _emit(on_event, {"type": "iteration", "iteration": iteration,
                                   "model": router_model if use_router else model})
            try:
                if use_router:
                    router_calls += 1
                    routed = await _router_message(client, {
                        "model": router_model, "messages": messages,
                        "tools": registry.schemas(), "temperature": temperature,
                        "max_tokens": router_max_tokens,
                    })
                    if routed is not None:
                        message = routed
                    else:
                        # No tool call, so what remains is prose -- the larger
                        # model's job. Nothing from the router reaches the
                        # history, so the answer is written as if the larger
                        # model had run the whole turn.
                        log.debug("agent: escalating iteration %d to %s to write "
                                  "the answer", iteration, model)
                        await _emit(on_event, {"type": "escalate",
                                               "iteration": iteration,
                                               "from": router_model, "to": model})
                        message = await one_call(model, max_tokens, stream_tokens)
                else:
                    message = await one_call(model, max_tokens, stream_tokens)
            except Exception as exc:  # noqa: BLE001 - upstream down, timeout, bad JSON
                model_ms = (time.perf_counter() - call_started) * 1000
                log.error("agent: inference call failed on iteration %d: %s", iteration, exc)
                steps.append(Step(
                    iteration=iteration, tool="final_answer", arguments={},
                    result=f"inference call failed: {type(exc).__name__}: {exc}",
                    latency_ms=model_ms, model_latency_ms=model_ms, ok=False,
                ))
                return AgentResult(
                    answer=(
                        f"I couldn't reach the model to answer this "
                        f"({type(exc).__name__}). Nothing was answered; this is an "
                        f"infrastructure failure, not a result."
                    ),
                    steps=steps, iterations=iteration, completed=False,
                    stop_reason="inference_error", model=model,
                    router_model=router_model, router_calls=router_calls,
                    total_ms=(time.perf_counter() - started) * 1000,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    usage_measured=usage_measured,
                )
            model_ms = (time.perf_counter() - call_started) * 1000
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                answer = (message.get("content") or "").strip()

                # A figure nothing produced is recall, not a result. Give the
                # model one chance to go and get it; see the guard's note above.
                # Per-figure, not per-turn. "Some tool succeeded" is not
                # evidence for *this* number: in the eval, search_filings
                # failed, calculate_ratio(net_margin) succeeded, and the model
                # invented a share count and an EPS that appeared nowhere in
                # the turn. See agent/grounding.py.
                unverified = unsupported_figures(answer, steps) if answer else []
                if unverified and grounding_retries < MAX_GROUNDING_RETRIES:
                    grounding_retries += 1
                    # Recorded as a failed step, not dropped: the discarded
                    # answer is the evidence that the guard fired, and a trace
                    # that hides it makes the extra iteration unexplainable.
                    steps.append(Step(
                        iteration=iteration, tool="ungrounded_answer",
                        arguments={"figures": [f.text for f in unverified]},
                        result=answer, latency_ms=model_ms,
                        model_latency_ms=model_ms, ok=False,
                    ))
                    log.warning(
                        "agent: discarded an answer on iteration %d; %d figure(s) "
                        "trace to no successful tool result: %s",
                        iteration, len(unverified),
                        ", ".join(f.text for f in unverified[:6]),
                    )
                    await _emit(on_event, {
                        "type": "discarded",
                        "iteration": iteration,
                        "reason": "ungrounded",
                        "draft": answer,
                        "figures": [f.text for f in unverified],
                    })
                    messages.append({"role": "assistant", "content": answer})
                    # Name the offending numbers. A generic "that was not
                    # grounded" leaves the model guessing which figure to fix,
                    # and it usually guesses the one that was fine.
                    messages.append({
                        "role": "user",
                        "content": UNGROUNDED_RETRY.format(
                            figures=", ".join(f.text for f in unverified[:6])
                        ),
                    })
                    # Reading "these figures trace to nothing, go and verify
                    # them" and deciding what to call is the reasoning step the
                    # router is not for.
                    escalate_next = True
                    continue

                # The retry is spent and figures are still unaccounted for.
                # The answer is not thrown away -- its prose is often right and
                # the caller is owed whatever was established -- but it is
                # never handed over as fact. It is prefixed with what could not
                # be verified, and `completed` is False so a caller can tell
                # this apart from a clean answer without reading the text.
                if unverified:
                    answer = UNVERIFIED_CAVEAT.format(
                        figures=", ".join(f.text for f in unverified[:6]),
                    ) + answer
                    log.warning(
                        "agent: answering with a caveat; %d figure(s) unverified "
                        "after the retry: %s", len(unverified),
                        ", ".join(f.text for f in unverified[:6]),
                    )
                    await _emit(on_event, {
                        "type": "unverified",
                        "iteration": iteration,
                        "figures": [f.text for f in unverified],
                    })

                steps.append(Step(
                    iteration=iteration, tool="final_answer",
                    arguments={"unverified": [f.text for f in unverified]}
                    if unverified else {},
                    result=answer, latency_ms=model_ms, model_latency_ms=model_ms,
                    ok=bool(answer) and not unverified,
                ))
                log.info("agent: answered on iteration %d after %d tool calls",
                         iteration, len([s for s in steps if s.tool != "final_answer"]))
                return AgentResult(
                    answer=answer or (
                        "The model returned an empty response. This is a failure, "
                        "not an answer."
                    ),
                    steps=steps, iterations=iteration,
                    completed=bool(answer) and not unverified,
                    stop_reason=("unverified_figures" if unverified else
                                 ("final_answer" if answer else "empty_response")),
                    model=model, router_model=router_model, router_calls=router_calls,
                    total_ms=(time.perf_counter() - started) * 1000,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    usage_measured=usage_measured,
                )

            # The assistant's tool-call message has to go into the history
            # verbatim: every tool message that follows is matched to it by
            # tool_call_id, and a model given results it never asked for will
            # either re-request them or ignore them.
            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })

            # Over-budget calls are dropped here, before any of them run, so
            # the ones that do run are a prefix of what the model asked for --
            # not a sample of it.
            over_budget = len(tool_calls) > max_tool_calls_per_iteration
            executed_calls = tool_calls[:max_tool_calls_per_iteration]
            if over_budget:
                skipped = [
                    (c.get("function") or {}).get("name") or "(unnamed)"
                    for c in tool_calls[max_tool_calls_per_iteration:]
                ]
                log.warning(
                    "agent: iteration=%d requested %d tool calls, budget is %d; "
                    "running the first %d and stopping. skipped: %s",
                    iteration, len(tool_calls), max_tool_calls_per_iteration,
                    len(executed_calls), ", ".join(skipped),
                )

            await _emit(on_event, {
                "type": "tool_start",
                "iteration": iteration,
                "tools": [
                    (c.get("function") or {}).get("name") or "(unnamed)"
                    for c in executed_calls
                ],
            })

            for call in executed_calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                arguments, parse_error = _parse_arguments(function.get("arguments"))

                if parse_error:
                    outcome = {"ok": False, "error": "bad_input", "message": parse_error}
                    step = Step(
                        iteration=iteration, tool=name or "(unnamed)", arguments={},
                        result=outcome, latency_ms=0.0, model_latency_ms=model_ms, ok=False,
                    )
                else:
                    called = await registry.call(name, arguments)
                    outcome = called.result
                    step = Step(
                        iteration=iteration, tool=name, arguments=arguments,
                        result=outcome, latency_ms=called.latency_ms,
                        model_latency_ms=model_ms, ok=called.ok,
                    )

                steps.append(step)
                # A failed call is the one case where the router demonstrably
                # cannot carry on: given a bad_input envelope it apologised
                # instead of retrying with the missing argument. The next
                # iteration goes to the larger model.
                if not step.ok:
                    escalate_next = True
                await _emit(on_event, {
                    "type": "tool_result",
                    "iteration": iteration,
                    "tool": step.tool,
                    "arguments": step.arguments,
                    "ok": step.ok,
                    "latency_ms": round(step.latency_ms, 1),
                    "result": _one_line(step.result, 400),
                })
                log.info("agent: iteration=%d tool=%s ok=%s latency=%.0fms args=%s",
                         iteration, step.tool, step.ok, step.latency_ms,
                         _one_line(step.arguments, 120))

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "name": name,
                    "content": json.dumps(compact_for_model(name, outcome), default=str),
                })

            if over_budget:
                # Same ending as running out of iterations: say so, and hand
                # back what the calls that did run established. Not a silent
                # truncation -- an answer written from a prefix of the evidence,
                # with no sign that the rest was dropped, is the thing this is
                # here to prevent.
                findings = _findings(steps)
                fields = dict(
                    requested=len(tool_calls),
                    budget=max_tool_calls_per_iteration,
                    executed=len(executed_calls),
                )
                answer = (
                    EXHAUSTED_TOOL_BUDGET.format(findings=findings, **fields)
                    if findings else
                    EXHAUSTED_TOOL_BUDGET_NO_FINDINGS.format(**fields)
                )
                steps.append(Step(
                    iteration=iteration, tool="tool_call_budget",
                    arguments={"requested": len(tool_calls),
                               "budget": max_tool_calls_per_iteration,
                               "skipped": skipped},
                    result=answer, latency_ms=0.0, model_latency_ms=0.0, ok=False,
                ))
                await _emit(on_event, {
                    "type": "budget",
                    "iteration": iteration,
                    "requested": len(tool_calls),
                    "budget": max_tool_calls_per_iteration,
                    "skipped": skipped,
                })
                return AgentResult(
                    answer=answer, steps=steps, iterations=iteration, completed=False,
                    stop_reason="tool_call_budget", model=model,
                    router_model=router_model, router_calls=router_calls,
                    total_ms=(time.perf_counter() - started) * 1000,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    usage_measured=usage_measured,
                )

        # Budget exhausted with tool calls still outstanding.
        findings = _findings(steps)
        answer = (
            EXHAUSTED_TEMPLATE.format(max_iterations=max_iterations, findings=findings)
            if findings else EXHAUSTED_NO_FINDINGS.format(max_iterations=max_iterations)
        )
        log.warning("agent: exhausted %d iterations without a final answer", max_iterations)
        steps.append(Step(
            iteration=max_iterations, tool="final_answer", arguments={},
            result=answer, latency_ms=0.0, model_latency_ms=0.0, ok=False,
        ))
        return AgentResult(
            answer=answer, steps=steps, iterations=max_iterations, completed=False,
            stop_reason="max_iterations", model=model,
            router_model=router_model, router_calls=router_calls,
            total_ms=(time.perf_counter() - started) * 1000,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            usage_measured=usage_measured,
        )
    finally:
        if owns_client:
            await client.aclose()
