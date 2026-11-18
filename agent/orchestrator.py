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

import asyncio
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
    LAST_TURN,
    EXHAUSTED_TOOL_BUDGET_NO_FINDINGS,
    system_prompt,
)
from agent.advice import (
    REDIRECTION,
    REDIRECTION_WITH_FINDINGS,
    advisory_spans,
)
from agent.grounding import ungrounded_operands, unsupported_figures
from agent.budget import fit_to_budget
from agent.untrusted import wrap_tool_result
from agent.tools import build_registry, compact_for_model
from core.config import settings
from tools.calculator import TOOL_NAME as CALCULATOR, is_reference, parse_operand
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
    "could not establish them. Do not assume a value in order to compute one. "
    "If one of them is a figure you worked out yourself, your own arithmetic is "
    "not accepted either: get it from the calculate tool and state the value it "
    "returns."
)

# Returned in place of running `calculate` when an operand is a number no tool
# returned this turn. See agent/grounding.py: a result of the calculator is
# evidence, so its operands have to be.
UNGROUNDED_OPERAND = (
    "calculate was not run. Operand {names} ({values}) {verb} not a figure any "
    "tool has returned in this turn, so nothing can be computed from {pronoun}. "
    "Pass a fact reference such as 'NVDA:revenue:2026', or first call the tool "
    "that returns the figure and then pass the number exactly as it was returned."
)


def _refused_operands(arguments: dict, steps) -> dict | None:
    """An error envelope if a `calculate` call names a number no tool returned."""
    numeric = {}
    for name in ("a", "b"):
        raw = arguments.get(name)
        if raw is None or is_reference(raw):
            continue
        value = parse_operand(raw)
        if value is not None:
            numeric[name] = (raw, value)
    missing = ungrounded_operands(numeric, steps) if numeric else []
    if not missing:
        return None
    return {
        "ok": False, "error": "bad_input",
        "message": UNGROUNDED_OPERAND.format(
            names=" and ".join(missing),
            values=", ".join(str(arguments[name]) for name in missing),
            verb="is" if len(missing) == 1 else "are",
            pronoun="it" if len(missing) == 1 else "them",
        ),
    }

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
# The final figure
# ─────────────────────────────────────────────────────────────────────────────
#
# The system prompt asks every answer to end with "Final figure: <number and
# unit>". It exists for whoever has to read *the* number out of the prose: the
# eval's scorer, which had been reading the first plausible number instead --
# the $171.25 price in "at $171.25 ... a P/E of 21.65x", the 71.07% in "71.07%
# against 48.79%, a difference of 22.28 points".
#
# It is a line of text and not a tool call, because Ollama ignores tool_choice
# and a final answer cannot be forced through one. It is read *after* both
# guards have run on the full text, line included, so the figure in it is
# grounding-checked like any other and is gone if the advice guard replaced
# the answer. The line is then removed from the answer the user sees and stored
# as `final_figure`. A model that does not write it costs nothing: the field is
# None and every consumer falls back to the prose.

_FINAL_FIGURE = re.compile(r"^[ \t*_>-]*final figure\s*[*_]*\s*:\s*[*_]*\s*(.*?)[ \t*_.]*$",
                           re.I | re.M)

# What a model writes when it has no figure. Kept as the literal "none" so a
# reader can tell "declined" from "did not follow the format" (None).
FINAL_FIGURE_NONE = "none"
_DECLINED = re.compile(r"^(?:none|n/?a|not available|unknown|-|—)$", re.I)


def split_final_figure(answer: str) -> tuple[str, str | None]:
    """(answer without its final-figure line, the figure as written or None).

    The last such line wins: a model that restates its figure after a
    correction means the second one. Every such line is removed from the text.
    """
    matches = list(_FINAL_FIGURE.finditer(answer or ""))
    if not matches:
        return answer, None
    figure = matches[-1].group(1).strip()
    text = _FINAL_FIGURE.sub("", answer).rstrip()
    if not figure or _DECLINED.match(figure):
        return text, FINAL_FIGURE_NONE
    return text, figure


# What the user reads when the model wrote its "Final figure:" line and nothing
# else. Removing the line then left an empty answer, which was reported as
# "The model returned an empty response. This is a failure, not an answer."
# while `final_figure` still carried the number: the user was told there was no
# answer and the eval scored one.
FIGURE_ONLY_ANSWER = "The answer is {figure}."


def with_a_sentence(answer: str) -> str:
    """`answer`, with a sentence stating its figure put first if the
    "Final figure:" line is all the model wrote.

    The sentence repeats the figure exactly as the line gives it, so it adds
    no number to the text: what the grounding guard decided about the line
    holds for the sentence. An answer with prose of its own, with no line, or
    with "Final figure: none" is returned unchanged.
    """
    prose, figure = split_final_figure(answer)
    if prose.strip() or figure in (None, FINAL_FIGURE_NONE):
        return answer
    return FIGURE_ONLY_ANSWER.format(figure=figure) + "\n" + answer


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


class InferenceDeadlineExceeded(Exception):
    """One model call ran past settings.inference_call_deadline and was stopped."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"stopped after {seconds:.0f}s, the per-call deadline")
        self.seconds = seconds


async def _within_deadline(call: Awaitable[Any], seconds: float) -> Any:
    """Await one model call, and stop it after `seconds` of wall-clock time.

    INFERENCE_READ_TIMEOUT cannot do this, because it bounds *silence*, not
    duration: every byte that arrives restarts it. Measured against a fake
    upstream sending one byte a second under a 2s read timeout, streamed and
    router calls never ended -- directly and through the real API proxy alike.
    (A silent upstream is caught by the read timeout at every layer; the gap is
    specifically a stream that keeps moving.)

    Cancellation lands inside the call, whose `finally` closes the HTTP response.
    The proxy sees the disconnect and closes its upstream request, so the
    inference server can stop generating for a caller that has gone.

    Only this timer's own expiry is reported as a deadline. A TimeoutError raised
    from inside the call is something else and propagates unchanged.
    """
    scope = asyncio.timeout(seconds)
    try:
        async with scope:
            return await call
    except TimeoutError:
        if scope.expired():
            raise InferenceDeadlineExceeded(seconds) from None
        raise


def _cached_prompt_tokens(usage: dict) -> int:
    """Prompt tokens a provider reports as served from its cache, or 0.

    OpenAI-compatible providers put it in usage.prompt_tokens_details. A
    provider that omits the block, or a local server that has no cache, is 0.
    """
    details = usage.get("prompt_tokens_details") or {}
    try:
        return int(details.get("cached_tokens") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _rate_limit_wait_ms(response) -> float:
    """Milliseconds the proxy spent waiting out a provider rate limit for this call."""
    try:
        return float(response.headers.get("x-rate-limit-wait-ms") or 0)
    except (TypeError, ValueError):
        return 0.0


async def _stream_message(client, payload, on_token, headers=None, waits=None) -> dict:
    """Run one streaming chat completion and return the assembled message.

    Returns the same shape the buffered path pulls out of `choices[0].message`.
    """
    content_parts: list[str] = []
    tool_calls: dict[int, dict] = {}

    request = client.build_request("POST", "/chat/completions",
                                   json={**payload, "stream": True}, headers=headers)
    response = await client.send(request, stream=True)
    if waits is not None:
        waits.append(_rate_limit_wait_ms(response))
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


async def _router_message(client, payload, headers=None, waits=None) -> dict | None:
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
                                   json={**payload, "stream": True}, headers=headers)
    response = await client.send(request, stream=True)
    if waits is not None:
        waits.append(_rate_limit_wait_ms(response))
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
    # Which provider served each model: "ollama" or a hosted one.
    provider: str = ""
    router_provider: str = ""
    # Part of total_ms that was the proxy waiting out a hosted provider's rate
    # limit, not the model working. Zero on a local provider.
    rate_limit_wait_ms: float = 0.0
    # The figure from the answer's "Final figure:" line, as written ("$215.9
    # billion"), FINAL_FIGURE_NONE when the model declined, or None when the
    # line was absent or the answer is not the model's own. See
    # split_final_figure.
    final_figure: str | None = None
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
    # Of prompt_tokens, how many the provider says it served from its prompt
    # cache. A hosted provider may not count these against its rate limits, so
    # this is what separates "tokens sent" from "tokens that used up the
    # allowance". Zero when the provider reports none or does not cache.
    cached_prompt_tokens: int = 0
    # One entry per model call that reported usage, in order:
    # {"prompt_tokens", "cached_prompt_tokens", "completion_tokens"}. The sums
    # above hide whether the first call of a turn missed the cache and the
    # rest hit it; this does not.
    call_usage: list[dict[str, int]] = field(default_factory=list)
    router_calls: int = 0
    # One entry per request sent to a model (router calls included), in order:
    # {"estimated_tokens", "budget", "trimmed", "over_budget"}. Estimated from
    # length before sending (agent/budget.py), so it is there even when the
    # provider rejects the request and reports no usage.
    request_sizes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tool_calls(self) -> list[Step]:
        return [step for step in self.steps if step.tool != "final_answer"]

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def charts(self) -> list[dict]:
        """Charts the agent drew this turn, from successful show_price_chart
        calls, in order. The chat renders these under the answer."""
        return [step.result["chart"] for step in self.steps
                if step.tool == "show_price_chart" and step.ok
                and isinstance(step.result, dict) and step.result.get("chart")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "iterations": self.iterations,
            "completed": self.completed,
            "stop_reason": self.stop_reason,
            "model": self.model,
            "router_model": self.router_model,
            "provider": self.provider,
            "router_provider": self.router_provider if self.router_model else "",
            "router_calls": self.router_calls,
            "final_figure": self.final_figure,
            "total_ms": round(self.total_ms, 1),
            "rate_limit_wait_ms": round(self.rate_limit_wait_ms, 1),
            "prompt_tokens": self.prompt_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "usage_measured": self.usage_measured,
            "call_usage": list(self.call_usage),
            "request_sizes": list(self.request_sizes),
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


def _pct(x) -> str:
    return f"{x * 100:.2f}%" if isinstance(x, (int, float)) else "n/a"


def _describe(step: Step) -> str:
    """One plain sentence for what a successful tool call established.

    Never the raw result: a user who reaches an exhaustion or redirection
    message is owed the facts, not a JSON dump (the live NVIDIA analysis of
    2026-11-18 showed one). Known tools say what they found, with the figures
    they returned; anything else is only named."""
    r = step.result if isinstance(step.result, dict) else {}
    a = step.arguments or {}
    t = step.tool
    who = r.get("ticker") or a.get("ticker") or a.get("company") or ""
    if t == "get_financial_fact" and r.get("formatted"):
        what = (r.get("description") or r.get("fact") or "figure").lower()
        src = f" (SEC filing {r['accession']})" if r.get("accession") else ""
        return f"{who} {what} for fiscal {r.get('fiscal_year')}: {r['formatted']}{src}."
    if t == "calculate_ratio" and r.get("formatted"):
        name = str(r.get("ratio", "ratio")).replace("_", " ")
        return f"{who} {name} (period ending {r.get('period_end')}): {r['formatted']}."
    if t == "calculate" and r.get("formula"):
        return f"Calculated: {r['formula']}."
    if t == "get_price_analytics" and "total_return" in r:
        return (f"{who} from {r.get('start')} to {r.get('end')}: total return {_pct(r.get('total_return'))}, "
                f"largest fall from a peak {_pct(r.get('max_drawdown'))}.")
    if t == "get_stock_price" and r.get("last"):
        last = r["last"] or {}
        when = last.get("date") or r.get("end_date")
        return f"{who} last closed at {last.get('close')} {r.get('currency') or ''} on {when}.".replace("  ", " ")
    if t == "get_trading_signal" and r.get("signal"):
        statement = (r.get("backtest") or {}).get("statement") or ""
        return f"The signal engine's output for {who} is {r['signal']} ({r.get('coverage')}). {statement}".strip()
    if t == "get_company_snapshot":
        parts = []
        fin = r.get("financials") or {}
        years = fin.get("years") or []
        if years:
            y = years[-1]
            label = f"fiscal {y['fiscal_year']}" if y.get("fiscal_year") else f"the year to {y.get('period_end')}"
            if y.get("revenue") is not None:
                parts.append(f"revenue in {label} was {y['revenue']:,} {fin.get('currency') or ''}".strip())
            if y.get("net_margin") is not None:
                parts.append(f"net margin {_pct(y['net_margin'])}")
        perf = r.get("performance") or {}
        if "total_return" in perf:
            parts.append(f"the stock returned {_pct(perf['total_return'])} over the last year")
        sig = r.get("signal") or {}
        if sig.get("signal"):
            parts.append(f"the signal engine's output is {sig['signal']} ({sig.get('coverage')})")
        return (f"{who}: " + "; ".join(parts) + ".") if parts else f"Looked up an overview of {who}."
    if t == "search_filings":
        return f"Searched {who or 'the filings'} for \"{a.get('query', '')}\": {r.get('count', 0)} passage(s) found."
    if t == "search_news":
        return f"Searched the news for \"{a.get('query', '')}\": {len(r.get('articles') or [])} headline(s)."
    if t == "show_price_chart":
        return f"Drew a price chart of {who}."
    # Any other tool: its top-level figures and short labels, as "name value"
    # pairs. Nested data is left out; the figures are what the user is owed
    # (tests/test_orchestrator.py, test_the_figures_survive_the_block).
    pairs = [f"{k.replace('_', ' ')} {v}" for k, v in r.items()
             if k not in ("ok", "ticker", "company", "source", "source_url")
             and not isinstance(v, bool)
             and (isinstance(v, (int, float)) or (isinstance(v, str) and len(v) <= 40))][:6]
    head = f"Called {t}" + (f" for {who}" if who else "")
    return f"{head}: {', '.join(pairs)}." if pairs else f"{head}."


def _findings(steps: list[Step]) -> str:
    """What the successful tool calls established, as plain sentences, for the
    exhaustion and redirection messages."""
    lines = [f"- {_describe(step)}" for step in steps
             if step.ok and step.tool not in ("final_answer", "ungrounded_answer")]
    return "\n".join(lines)


async def run_agent(
    question: str,
    history: list[dict[str, str]] | None = None,
    *,
    registry: Registry | None = None,
    model: str | None = None,
    router_model: str | None = None,
    provider: str | None = None,
    router_provider: str | None = None,
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
        provider: which provider serves `model` -- "ollama" or a hosted one.
            Defaults to `settings.agent_provider`. Sent to the app's proxy as a
            header; the proxy holds the key.
        router_provider: the same for `router_model`. Defaults to
            `settings.router_provider`, which is the answer model's unless set.
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
    provider = provider or settings.agent_provider
    router_provider = router_provider or settings.router_provider
    if router_model == model and router_provider == provider:
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
    prompt_tokens = completion_tokens = cached_prompt_tokens = 0
    usage_measured = False
    call_usage: list[dict[str, int]] = []
    # The compact result behind each tool message, by tool_call_id, so the
    # budget can trim it and wrap it again (agent/budget.py).
    tool_results: dict[str, tuple[str, dict]] = {}
    request_sizes: list[dict[str, Any]] = []

    def within_budget(call_model: str, with_tools: bool = True) -> list[dict[str, Any]]:
        """The messages to send to `call_model`: the largest tool results
        trimmed if the request would be over the budget. Logs the size. The
        last step is sent without tools, so its schemas are not counted."""
        sent, report = fit_to_budget(messages, registry.schemas() if with_tools else [],
                                     settings.agent_request_token_budget,
                                     tool_results, wrap_tool_result)
        entry = {"estimated_tokens": report["estimated_tokens"], "budget": report["budget"],
                 "trimmed": len(report["trimmed"]), "over_budget": bool(report.get("over_budget"))}
        request_sizes.append(entry)
        log.info("agent: request to %s, about %d tokens (budget %d)%s%s", call_model,
                 entry["estimated_tokens"], entry["budget"],
                 f", {entry['trimmed']} tool result(s) trimmed" if entry["trimmed"] else "",
                 ", still over budget" if entry["over_budget"] else "")
        return sent

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

    async def one_call(call_model: str, cap: int, stream: bool, with_tools: bool = True) -> dict:
        """One chat completion, stopped at the per-call deadline.

        Each call gets the whole deadline: an escalation or a grounding retry is a
        new call with a new clock, so a turn of several legitimate calls is never
        cut short by the sum of them.
        """
        return await _within_deadline(_one_call(call_model, cap, stream, with_tools),
                                      settings.inference_call_deadline)

    # Which provider the proxy should use for each model. A header per call
    # rather than on the client, because the two models can be on different
    # providers. A server that is not the app's proxy ignores it.
    # Time the proxy spent waiting out a hosted provider's rate limit, per call.
    # Inside total_ms, and reported beside it so the two can be told apart.
    waits: list[float] = []
    answer_headers = {"X-Inference-Provider": provider}
    router_headers = {"X-Inference-Provider": router_provider}

    async def _one_call(call_model: str, cap: int, stream: bool, with_tools: bool = True) -> dict:
        """One chat completion. Accumulates usage; raises on transport failure."""
        nonlocal prompt_tokens, completion_tokens, cached_prompt_tokens, usage_measured
        payload = {
            "model": call_model,
            "messages": within_budget(call_model, with_tools),
            "temperature": temperature,
            "max_tokens": cap,
            "stream": False,
        }
        # The last step is for the answer: it is sent with no tools, so the
        # model writes from what it has instead of asking for more.
        if with_tools:
            payload["tools"] = registry.schemas()
        if stream:
            return await _stream_message(client, payload, on_token, answer_headers, waits)
        response = await client.post("/chat/completions", json=payload,
                                     headers=answer_headers)
        waits.append(_rate_limit_wait_ms(response))
        response.raise_for_status()
        body = response.json()
        # Ollama reports usage on the buffered path only. Summed across
        # iterations, because one question is several calls and the per-call
        # number is not what anything wants.
        usage = body.get("usage") or {}
        if usage:
            usage_measured = True
            sent = int(usage.get("prompt_tokens") or 0)
            written = int(usage.get("completion_tokens") or 0)
            cached = _cached_prompt_tokens(usage)
            prompt_tokens += sent
            completion_tokens += written
            cached_prompt_tokens += cached
            call_usage.append({"prompt_tokens": sent, "cached_prompt_tokens": cached,
                               "completion_tokens": written})
        return ((body.get("choices") or [{}])[0]).get("message") or {}

    try:
        for iteration in range(1, max_iterations + 1):
            # The router handles tool selection; agent_model writes the answer.
            # Two things take routing away from it. A tool call that failed last
            # iteration needs recovery, which the small model measurably cannot
            # do -- see settings.agent_router_model. And a grounding retry is an
            # instruction to go and verify a figure, which is the same kind of
            # work.
            # The last iteration is kept for writing the answer: no tools are
            # offered, so the model cannot spend it on one more lookup. On the
            # live site (2026-11-18) a broad question spent all five steps on
            # lookups and never answered.
            last = iteration == max_iterations
            use_router = bool(router_model) and not escalate_next and not last
            escalate_next = False
            if last:
                messages.append({"role": "user", "content": LAST_TURN})

            call_started = time.perf_counter()
            await _emit(on_event, {"type": "iteration", "iteration": iteration,
                                   "model": router_model if use_router else model})
            try:
                if use_router:
                    router_calls += 1
                    routed = await _within_deadline(_router_message(client, {
                        "model": router_model, "messages": within_budget(router_model),
                        "tools": registry.schemas(), "temperature": temperature,
                        "max_tokens": router_max_tokens,
                    }, router_headers, waits), settings.inference_call_deadline)
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
                    message = await one_call(model, max_tokens, stream_tokens, with_tools=not last)
            except Exception as exc:  # noqa: BLE001 - upstream down, timeout, bad JSON
                model_ms = (time.perf_counter() - call_started) * 1000
                if isinstance(exc, InferenceDeadlineExceeded):
                    # Not "couldn't reach the model": it was reached, and it was
                    # still going. Saying so is what lets someone tell a slow
                    # host from a dead one.
                    log.error("agent: inference call on iteration %d stopped at the "
                              "%.0fs deadline", iteration, exc.seconds)
                    failure = f"inference call stopped at the {exc.seconds:.0f}s per-call deadline"
                    answer = (
                        f"The model did not finish within {exc.seconds:.0f} seconds, so "
                        f"the call was stopped. Nothing was answered; this is an "
                        f"infrastructure failure, not a result."
                    )
                    if any(s.ok and s.tool != "final_answer" for s in steps):
                        answer += " The tool results gathered before it are kept in the trace."
                else:
                    log.error("agent: inference call failed on iteration %d: %s", iteration, exc)
                    failure = f"inference call failed: {type(exc).__name__}: {exc}"
                    answer = (
                        f"I couldn't reach the model to answer this "
                        f"({type(exc).__name__}). Nothing was answered; this is an "
                        f"infrastructure failure, not a result."
                    )
                steps.append(Step(
                    iteration=iteration, tool="final_answer", arguments={},
                    result=failure,
                    latency_ms=model_ms, model_latency_ms=model_ms, ok=False,
                ))
                return AgentResult(
                    answer=answer,
                    steps=steps, iterations=iteration, completed=False,
                    stop_reason="inference_error", model=model,
                    router_model=router_model, router_calls=router_calls,
                    provider=provider, router_provider=router_provider,
                    rate_limit_wait_ms=sum(waits),
                    total_ms=(time.perf_counter() - started) * 1000,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    usage_measured=usage_measured,
                    cached_prompt_tokens=cached_prompt_tokens, call_usage=call_usage, request_sizes=request_sizes,
                )
            model_ms = (time.perf_counter() - call_started) * 1000
            # Tools were not offered on the last step; a call made anyway is
            # not run.
            tool_calls = [] if last else (message.get("tool_calls") or [])
            # A last step that wrote nothing (it asked for a tool anyway) ends
            # like any exhausted run: with the findings in plain sentences.
            if last and not (message.get("content") or "").strip():
                break

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

                # After the grounding check and before the caveat and the
                # advice check, so both of those see the sentence the user
                # will read. See with_a_sentence.
                answer = with_a_sentence(answer)

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

                # The advice backstop. Last, because it is the only check that
                # replaces the answer outright rather than annotating it -- and
                # it runs on whatever the earlier guards settled on, caveat
                # included, since a hedged recommendation is still a
                # recommendation.
                #
                # Unlike the grounding guard there is no retry. A model that has
                # decided to recommend something does not un-decide when asked
                # again; it rephrases, which produces the same advice past a
                # pattern that no longer matches it. Replacing the text is the
                # mechanism. See agent/advice.py.
                advice = advisory_spans(answer) if answer else []
                if advice:
                    log.warning(
                        "agent: blocked an advisory answer on iteration %d; "
                        "%d phrase(s): %s", iteration, len(advice),
                        "; ".join(f"[{a.kind}] {a.text}" for a in advice[:4]),
                    )
                    steps.append(Step(
                        iteration=iteration, tool="advisory_answer",
                        arguments={"phrases": [a.text for a in advice],
                                   "kinds": sorted({a.kind for a in advice})},
                        # The blocked draft is kept, not dropped: a trace that
                        # hides what was replaced cannot be reviewed, and this
                        # is exactly the decision someone will want to audit.
                        result=answer, latency_ms=model_ms,
                        model_latency_ms=model_ms, ok=False,
                    ))
                    await _emit(on_event, {
                        "type": "blocked",
                        "iteration": iteration,
                        "reason": "investment_advice",
                        "phrases": [a.text for a in advice],
                        "draft": answer,
                    })
                    findings = _findings(steps)
                    answer = (
                        REDIRECTION_WITH_FINDINGS.format(findings=findings)
                        if findings else REDIRECTION
                    )

                # Read after both guards, so the figure was checked with the
                # rest of the text; a replaced answer has no line to read.
                answer, final_figure = split_final_figure(answer)

                steps.append(Step(
                    iteration=iteration, tool="final_answer",
                    arguments={"unverified": [f.text for f in unverified]}
                    if unverified else {},
                    result=answer, latency_ms=model_ms, model_latency_ms=model_ms,
                    ok=bool(answer) and not unverified and not advice,
                ))
                log.info("agent: answered on iteration %d after %d tool calls",
                         iteration, len([s for s in steps if s.tool != "final_answer"]))
                return AgentResult(
                    answer=answer or (
                        "The model returned an empty response. This is a failure, "
                        "not an answer."
                    ),
                    steps=steps, iterations=iteration,
                    completed=bool(answer) and not unverified and not advice,
                    final_figure=final_figure,
                    stop_reason=("investment_advice" if advice else
                                 ("unverified_figures" if unverified else
                                  ("final_answer" if answer else "empty_response"))),
                    model=model, router_model=router_model, router_calls=router_calls,
                    provider=provider, router_provider=router_provider,
                    rate_limit_wait_ms=sum(waits),
                    total_ms=(time.perf_counter() - started) * 1000,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    usage_measured=usage_measured,
                    cached_prompt_tokens=cached_prompt_tokens, call_usage=call_usage, request_sizes=request_sizes,
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
                elif name == CALCULATOR and (refused := _refused_operands(arguments, steps)):
                    # Before the tool, not inside it: only the loop knows what
                    # this turn's tools have returned.
                    outcome = refused
                    step = Step(
                        iteration=iteration, tool=name, arguments=arguments,
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

                call_id = call.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                compact = compact_for_model(name, outcome)
                if isinstance(compact, dict):
                    tool_results[call_id] = (name, compact)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    # Enclosed rather than pasted in raw. A filing excerpt or a
                    # news headline is written by someone who is not operating
                    # this agent, and arrives in the same conversation as the
                    # system prompt. See agent/untrusted.py.
                    "content": wrap_tool_result(name, compact),
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
                    provider=provider, router_provider=router_provider,
                    rate_limit_wait_ms=sum(waits),
                    total_ms=(time.perf_counter() - started) * 1000,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    usage_measured=usage_measured,
                    cached_prompt_tokens=cached_prompt_tokens, call_usage=call_usage, request_sizes=request_sizes,
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
                    provider=provider, router_provider=router_provider,
                    rate_limit_wait_ms=sum(waits),
            total_ms=(time.perf_counter() - started) * 1000,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            usage_measured=usage_measured,
                    cached_prompt_tokens=cached_prompt_tokens, call_usage=call_usage, request_sizes=request_sizes,
        )
    finally:
        if owns_client:
            await client.aclose()
