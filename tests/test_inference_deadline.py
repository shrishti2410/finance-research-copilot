"""The per-call wall-clock deadline: a stream that never stops trickling is stopped.

INFERENCE_READ_TIMEOUT bounds the silence *between* bytes. An upstream that sends
one byte every 60 seconds never trips a 300s read timeout, so until
settings.inference_call_deadline existed, nothing ended that call.

These run against a fake upstream on a real socket, not httpx's MockTransport.
MockTransport enforces no network timeouts at all, so it could not show the read
timeout failing to fire -- and that is the half of the claim that matters.

The first test is the literal scenario and takes a little over two minutes. The
rest are scaled down (a byte every 0.25s under a 1s read timeout), which keeps the
property that makes the case hard: a byte always arrives before the read timer
runs out.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time

import pytest
from pydantic import ValidationError

from agent.orchestrator import InferenceDeadlineExceeded, _within_deadline, run_agent
from core.config import Settings, settings
from tools.base import ok as tool_ok
from tools.registry import Registry


# ─────────────────────────────────────────────────────────────────────────────
# A fake inference server on a real socket
# ─────────────────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sse(*chunks: dict) -> str:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


TOOL_CALL_STREAM = sse({"choices": [{"delta": {"tool_calls": [{
    "index": 0, "id": "call-1", "type": "function",
    "function": {"name": "lookup_revenue", "arguments": json.dumps({"ticker": "NVDA"})},
}]}}]})

TOOL_CALL_JSON = {"choices": [{"finish_reason": "tool_calls", "message": {
    "role": "assistant", "content": "",
    "tool_calls": [{"id": "call-1", "type": "function", "function": {
        "name": "lookup_revenue", "arguments": json.dumps({"ticker": "NVDA"})}}],
}}]}

ANSWER_JSON = {"choices": [{"finish_reason": "stop", "message": {
    "role": "assistant", "content": "NVIDIA reports its revenue in its annual filing.",
}}]}


class Upstream:
    """An OpenAI-shaped server. Each request takes the next behaviour in `script`
    (the last one repeats):

        ("sse", text)             a complete streamed response
        ("json", (delay, body))   a buffered response after `delay` seconds
        ("trickle", interval)     headers, then one body byte every `interval`
                                  seconds until the client goes away
    """

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.trickled = 0
        self.disconnected_at: float | None = None
        self.port = _free_port()
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def serve() -> None:
            asyncio.set_event_loop(self._loop)
            self._server = self._loop.run_until_complete(
                asyncio.start_server(self._handle, "127.0.0.1", self.port))
            ready.set()
            self._loop.run_forever()

        threading.Thread(target=serve, daemon=True).start()
        assert ready.wait(10), "fake upstream did not start"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        """Stop listening and cancel open handlers before stopping the loop.

        Stopping the loop alone leaves the pending accept behind, and asyncio
        reports it as a destroyed task in whichever test runs next.
        """
        async def shutdown() -> None:
            self._server.close()
            for task in asyncio.all_tasks():
                if task is not asyncio.current_task():
                    task.cancel()
            await asyncio.sleep(0)
            self._loop.stop()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(5)
        except Exception:  # noqa: BLE001 - the loop stopping mid-await is the goal
            pass

    def wait_for_disconnect(self, seconds: float = 5.0) -> float | None:
        deadline = time.monotonic() + seconds
        while self.disconnected_at is None and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.disconnected_at

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = re.search(rb"content-length:\s*(\d+)", head, re.I)
            if length:
                await reader.readexactly(int(length.group(1)))
            kind, arg = self.script.pop(0) if len(self.script) > 1 else self.script[0]

            if kind in ("sse", "json"):
                if kind == "json":
                    delay, body = arg
                    await asyncio.sleep(delay)
                    payload, ctype = json.dumps(body).encode(), b"application/json"
                else:
                    payload, ctype = arg.encode(), b"text/event-stream"
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: " + ctype
                             + b"\r\ncontent-length: " + str(len(payload)).encode()
                             + b"\r\n\r\n" + payload)
                await writer.drain()
                return

            writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                         b"transfer-encoding: chunked\r\n\r\n")
            await writer.drain()
            gone = asyncio.ensure_future(reader.read(1))      # EOF when the client closes
            while True:
                done, _ = await asyncio.wait({gone}, timeout=arg)
                if done:
                    self.disconnected_at = time.monotonic()
                    return
                writer.write(b"1\r\n \r\n")                   # one body byte, no newline
                await writer.drain()
                self.trickled += 1
        except (ConnectionError, asyncio.IncompleteReadError):
            if self.disconnected_at is None:
                self.disconnected_at = time.monotonic()
        finally:
            writer.close()


def lookup_revenue(ticker: str) -> dict:
    """Look up a company's reported revenue.

    Args:
        ticker: the company's ticker symbol.
    """
    # The envelope matters: the registry reads success from "ok", as it does for
    # every real tool. A bare dict is recorded as a failed call.
    return tool_ok(ticker=ticker, revenue=130497000000, source="annual filing")


def ask(**kwargs):
    registry = Registry()
    registry.register(lookup_revenue)
    options = {"model": "answer-model", "router_model": "", "stream_tokens": True}
    return run_agent("What was NVIDIA's revenue?", registry=registry, **{**options, **kwargs})


def assert_stopped_honestly(result, deadline: float, tool_ran: bool = True) -> None:
    """An inference_error like any other: clean stop, trace kept, a true message."""
    assert result.stop_reason == "inference_error"
    assert result.completed is False

    tool_steps = [s for s in result.steps if s.tool == "lookup_revenue"]
    assert len(tool_steps) == (1 if tool_ran else 0)
    if tool_ran:
        assert tool_steps[0].ok, "the tool result gathered before the stall must survive"

    final = result.steps[-1]
    assert final.tool == "final_answer" and final.ok is False
    assert final.result == f"inference call stopped at the {deadline:.0f}s per-call deadline"

    assert f"did not finish within {deadline:.0f} seconds" in result.answer
    assert ("tool results gathered before it are kept" in result.answer) is tool_ran
    for leaked in ("TimeoutError", "CancelledError", "couldn't reach"):
        assert leaked not in result.answer


# ─────────────────────────────────────────────────────────────────────────────
# 1. the literal scenario
# ─────────────────────────────────────────────────────────────────────────────

def test_one_byte_a_minute_under_the_real_read_timeout_is_stopped_at_the_deadline(monkeypatch):
    """A tool call succeeds, then the answer stream trickles one byte every 60s.

    The read timeout is the production 300s, so it cannot fire. The deadline is
    130s, past two trickled bytes, so the bytes demonstrably did not extend it.
    """
    assert settings.inference_read_timeout == 300.0
    deadline = 130.0
    upstream = Upstream(("sse", TOOL_CALL_STREAM), ("trickle", 60.0))
    monkeypatch.setattr(settings, "agent_inference_base_url", upstream.base_url)
    monkeypatch.setattr(settings, "inference_call_deadline", deadline)
    try:
        started = time.monotonic()
        result = asyncio.run(ask())
        elapsed = time.monotonic() - started
        disconnected = upstream.wait_for_disconnect()
    finally:
        upstream.close()

    assert deadline <= elapsed < deadline + 10, f"ended after {elapsed:.1f}s"
    assert upstream.trickled == 2, "the upstream was still sending when it was cut off"
    assert disconnected is not None and disconnected - started < deadline + 10, \
        "the connection must be closed, not abandoned"
    assert_stopped_honestly(result, deadline)


# ─────────────────────────────────────────────────────────────────────────────
# 2. the same shape, scaled down
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def trickling(monkeypatch):
    """A byte every 0.25s under a 1s read timeout: never silent long enough to trip it."""
    upstream = Upstream(("sse", TOOL_CALL_STREAM), ("trickle", 0.25))
    monkeypatch.setattr(settings, "agent_inference_base_url", upstream.base_url)
    monkeypatch.setattr(settings, "inference_read_timeout", 1.0)
    yield upstream
    upstream.close()


def test_without_the_deadline_the_read_timeout_never_ends_a_trickling_stream(trickling, monkeypatch):
    """The control. Without this, the tests below could pass for the wrong reason."""
    monkeypatch.setattr(settings, "inference_call_deadline", 3600.0)

    async def bounded():
        return await asyncio.wait_for(ask(), timeout=5.0)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bounded())
    assert trickling.trickled >= 15, "bytes kept arriving the whole time"


def test_with_the_deadline_the_same_stream_ends_at_the_bound(trickling, monkeypatch):
    monkeypatch.setattr(settings, "inference_call_deadline", 2.0)

    started = time.monotonic()
    result = asyncio.run(ask())
    elapsed = time.monotonic() - started

    assert 2.0 <= elapsed < 3.5, f"ended after {elapsed:.2f}s"
    assert trickling.wait_for_disconnect() is not None
    assert_stopped_honestly(result, 2.0)


def test_a_buffered_call_is_bounded_too(monkeypatch):
    upstream = Upstream(("json", (0, TOOL_CALL_JSON)), ("trickle", 0.25))
    monkeypatch.setattr(settings, "agent_inference_base_url", upstream.base_url)
    monkeypatch.setattr(settings, "inference_read_timeout", 1.0)
    monkeypatch.setattr(settings, "inference_call_deadline", 2.0)
    try:
        started = time.monotonic()
        result = asyncio.run(ask(stream_tokens=False))
        elapsed = time.monotonic() - started
    finally:
        upstream.close()

    assert 2.0 <= elapsed < 3.5
    assert_stopped_honestly(result, 2.0)


def test_the_router_call_is_bounded_too(monkeypatch):
    """The routing step is its own streamed call, and it trickles here from the start."""
    upstream = Upstream(("trickle", 0.25))
    monkeypatch.setattr(settings, "agent_inference_base_url", upstream.base_url)
    monkeypatch.setattr(settings, "inference_read_timeout", 1.0)
    monkeypatch.setattr(settings, "inference_call_deadline", 1.5)
    try:
        started = time.monotonic()
        result = asyncio.run(ask(router_model="router-model"))
        elapsed = time.monotonic() - started
    finally:
        upstream.close()

    assert 1.5 <= elapsed < 3.0
    assert_stopped_honestly(result, 1.5, tool_ran=False)


def test_the_upstream_is_released_through_the_real_api_proxy(monkeypatch):
    """The deadline lives in the agent, so the inference server only stops if the
    disconnect makes it through the proxy. This runs the real app on a real port."""
    import uvicorn

    from api.main import app

    upstream = Upstream(("sse", TOOL_CALL_STREAM), ("trickle", 0.25))
    monkeypatch.setattr(settings, "inference_base_url", upstream.base_url)
    monkeypatch.setattr(settings, "inference_read_timeout", 1.0)
    monkeypatch.setattr(settings, "inference_call_deadline", 2.0)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        ready_by = time.monotonic() + 30
        while not server.started:
            assert time.monotonic() < ready_by, "API app did not start"
            time.sleep(0.05)
        monkeypatch.setattr(settings, "agent_inference_base_url", f"http://127.0.0.1:{port}/v1")

        started = time.monotonic()
        result = asyncio.run(ask())
        elapsed = time.monotonic() - started
        disconnected = upstream.wait_for_disconnect()
    finally:
        server.should_exit = True
        thread.join(10)
        upstream.close()

    assert 2.0 <= elapsed < 3.5
    assert disconnected is not None and disconnected - started < 5.0, \
        "the proxy must close its upstream request when the agent gives up"
    assert_stopped_honestly(result, 2.0)


# ─────────────────────────────────────────────────────────────────────────────
# 3. what the deadline must not do
# ─────────────────────────────────────────────────────────────────────────────

def test_each_call_gets_its_own_clock_not_one_per_turn(monkeypatch):
    """Two calls of 1.2s each under a 2s deadline: 2.4s in total, and not a failure."""
    upstream = Upstream(("json", (1.2, TOOL_CALL_JSON)), ("json", (1.2, ANSWER_JSON)))
    monkeypatch.setattr(settings, "agent_inference_base_url", upstream.base_url)
    monkeypatch.setattr(settings, "inference_call_deadline", 2.0)
    try:
        result = asyncio.run(ask(stream_tokens=False))
    finally:
        upstream.close()

    assert result.stop_reason != "inference_error"
    assert result.completed is True
    assert result.total_ms > 2000


def test_a_timeout_error_from_inside_the_call_is_not_reported_as_the_deadline():
    async def fails():
        raise TimeoutError("raised by something else")

    with pytest.raises(TimeoutError, match="raised by something else") as caught:
        asyncio.run(_within_deadline(fails(), 60.0))
    assert not isinstance(caught.value, InferenceDeadlineExceeded)


def test_the_deadline_is_explicit_and_must_be_positive():
    assert settings.inference_call_deadline == 900.0
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            Settings(inference_call_deadline=bad)
