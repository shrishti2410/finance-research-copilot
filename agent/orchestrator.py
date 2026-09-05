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
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from agent.prompts import EXHAUSTED_NO_FINDINGS, EXHAUSTED_TEMPLATE, system_prompt
from agent.tools import build_registry, compact_for_model
from core.config import settings
from tools.registry import Registry

log = logging.getLogger(__name__)

MAX_ITERATIONS = 5

# How much of a result is rendered into the trace table. The full value is kept
# on the step and stored with the message; this is only what fits on a screen.
TABLE_RESULT_CHARS = 88


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
    total_ms: float = 0.0

    @property
    def tool_calls(self) -> list[Step]:
        return [step for step in self.steps if step.tool != "final_answer"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "iterations": self.iterations,
            "completed": self.completed,
            "stop_reason": self.stop_reason,
            "model": self.model,
            "total_ms": round(self.total_ms, 1),
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
    max_iterations: int = MAX_ITERATIONS,
    client: httpx.AsyncClient | None = None,
    today: date | None = None,
    temperature: float = 0.0,
) -> AgentResult:
    """Answer `question`, calling tools as needed, within `max_iterations` turns.

    Args:
        question: the user's message.
        history: prior turns as [{"role", "content"}], oldest first.
        registry: tools to expose. Defaults to the four in `agent.tools`.
        model: model id. Defaults to `settings.agent_model`.
        client: an httpx client to reuse. One is created and closed if omitted.
        today: overrides the date given to the model, for reproducible tests.
        temperature: 0 by default -- tool selection should not be a dice roll.
    """
    registry = registry or build_registry()
    model = model or settings.agent_model
    started = time.perf_counter()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt(max_iterations, today)}
    ]
    messages += list(history or [])
    messages.append({"role": "user", "content": question})

    steps: list[Step] = []
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

    try:
        for iteration in range(1, max_iterations + 1):
            payload = {
                "model": model,
                "messages": messages,
                "tools": registry.schemas(),
                "temperature": temperature,
                "stream": False,
            }

            call_started = time.perf_counter()
            try:
                response = await client.post("/chat/completions", json=payload)
                response.raise_for_status()
                body = response.json()
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
                    total_ms=(time.perf_counter() - started) * 1000,
                )
            model_ms = (time.perf_counter() - call_started) * 1000

            choice = (body.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                answer = (message.get("content") or "").strip()
                steps.append(Step(
                    iteration=iteration, tool="final_answer", arguments={},
                    result=answer, latency_ms=model_ms, model_latency_ms=model_ms,
                    ok=bool(answer),
                ))
                log.info("agent: answered on iteration %d after %d tool calls",
                         iteration, len([s for s in steps if s.tool != "final_answer"]))
                return AgentResult(
                    answer=answer or (
                        "The model returned an empty response. This is a failure, "
                        "not an answer."
                    ),
                    steps=steps, iterations=iteration, completed=bool(answer),
                    stop_reason="final_answer" if answer else "empty_response",
                    model=model, total_ms=(time.perf_counter() - started) * 1000,
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

            for call in tool_calls:
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
                log.info("agent: iteration=%d tool=%s ok=%s latency=%.0fms args=%s",
                         iteration, step.tool, step.ok, step.latency_ms,
                         _one_line(step.arguments, 120))

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "name": name,
                    "content": json.dumps(compact_for_model(name, outcome), default=str),
                })

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
            total_ms=(time.perf_counter() - started) * 1000,
        )
    finally:
        if owns_client:
            await client.aclose()
