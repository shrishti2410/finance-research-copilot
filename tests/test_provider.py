"""The pluggable provider: settings, the proxy's routing, and what the agent sends.

Nothing here reaches a real provider. The hosted upstream is an httpx
MockTransport, so the suite needs no key and CI stays local.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent.orchestrator import run_agent
from api import inference_proxy
from core.config import Settings, settings
from tools.base import ok
from tools.registry import Registry

FAKE_KEY = "test-key-not-a-real-one"


# ── settings ──────────────────────────────────────────────────────────────────

def test_the_default_provider_is_local_and_needs_no_key():
    s = Settings(_env_file=None, groq_api_key="")
    assert s.agent_provider == "ollama" and s.router_provider == "ollama"


def test_the_router_provider_follows_the_answer_provider_unless_set():
    s = Settings(_env_file=None, agent_provider="groq", groq_api_key=FAKE_KEY)
    assert s.router_provider == "groq"
    s = Settings(_env_file=None, agent_provider="ollama", agent_router_provider="groq",
                 groq_api_key=FAKE_KEY)
    assert (s.agent_provider, s.router_provider) == ("ollama", "groq")


@pytest.mark.parametrize("fields", [
    {"agent_provider": "groq"},
    {"agent_router_provider": "groq"},
])
def test_a_hosted_provider_without_its_key_refuses_to_start(fields):
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, groq_api_key="", **fields)
    assert "GROQ_API_KEY" in str(caught.value)


def test_an_unknown_provider_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_provider="openai")


def test_an_empty_internal_token_is_generated_not_left_empty():
    s = Settings(_env_file=None, internal_token="")
    assert len(s.internal_token) >= 32


def test_the_suite_itself_is_pinned_to_the_local_provider():
    """tests/conftest.py: whatever .env says, tests do not call a hosted provider."""
    assert settings.agent_provider == "ollama" and settings.router_provider == "ollama"


# ── the proxy ─────────────────────────────────────────────────────────────────

@pytest.fixture
def proxy(monkeypatch):
    """The proxy with a fake local upstream and a fake hosted one, recording both."""
    seen = {"ollama": [], "groq": []}

    def upstream(name):
        def handler(request: httpx.Request) -> httpx.Response:
            seen[name].append(request)
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": f"from {name}"}}], "served_by": name})
        return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url=f"http://{name}.invalid/v1")

    monkeypatch.setattr(inference_proxy, "_client", upstream("ollama"))
    monkeypatch.setattr(inference_proxy, "_hosted", {"groq": upstream("groq")})
    monkeypatch.setattr(settings, "groq_api_key", FAKE_KEY)
    monkeypatch.setattr(settings, "internal_token", "the-process-token")

    app = FastAPI()
    app.include_router(inference_proxy.router, prefix="/v1")
    return TestClient(app), seen


BODY = {"model": "some-model", "messages": [{"role": "user", "content": "hi"}]}
INTERNAL = {"X-Internal-Token": "the-process-token"}


def test_no_header_goes_to_the_local_default(proxy):
    client, seen = proxy
    assert client.post("/v1/chat/completions", json=BODY).json()["served_by"] == "ollama"
    assert len(seen["ollama"]) == 1 and not seen["groq"]


def test_an_internal_call_naming_the_hosted_provider_reaches_it(proxy):
    client, seen = proxy
    response = client.post("/v1/chat/completions", json=BODY,
                           headers={**INTERNAL, "X-Inference-Provider": "groq"})
    assert response.json()["served_by"] == "groq"
    assert len(seen["groq"]) == 1 and not seen["ollama"]


@pytest.mark.parametrize("headers", [
    {"X-Inference-Provider": "groq"},
    {"X-Inference-Provider": "groq", "X-Internal-Token": "a-guess"},
])
def test_an_outside_caller_cannot_use_the_hosted_provider(proxy, headers):
    """/v1 needs no login. The operator's key must not serve whoever asks."""
    client, seen = proxy
    response = client.post("/v1/chat/completions", json=BODY, headers=headers)
    assert response.status_code == 403
    assert not seen["groq"] and not seen["ollama"]


def test_an_unknown_provider_is_a_400_that_names_the_known_ones(proxy):
    client, _ = proxy
    response = client.post("/v1/chat/completions", json=BODY,
                           headers={**INTERNAL, "X-Inference-Provider": "acme"})
    assert response.status_code == 400 and "groq" in response.json()["detail"]


def test_a_hosted_provider_with_no_key_is_a_503_that_does_not_leak(proxy, monkeypatch):
    client, _ = proxy
    monkeypatch.setattr(inference_proxy, "_hosted", {})
    response = client.post("/v1/chat/completions", json=BODY,
                           headers={**INTERNAL, "X-Inference-Provider": "groq"})
    assert response.status_code == 503
    assert ".env" in response.json()["detail"] and FAKE_KEY not in response.text


def test_the_local_default_model_is_not_sent_to_a_hosted_provider(proxy):
    client, seen = proxy
    response = client.post("/v1/chat/completions", json={"messages": BODY["messages"]},
                           headers={**INTERNAL, "X-Inference-Provider": "groq"})
    assert response.status_code == 400 and not seen["groq"]


def test_the_internal_token_and_provider_header_are_not_forwarded_upstream(proxy):
    client, seen = proxy
    client.post("/v1/chat/completions", json=BODY,
                headers={**INTERNAL, "X-Inference-Provider": "groq"})
    sent = seen["groq"][0].headers
    assert "x-internal-token" not in sent and "x-inference-provider" not in sent


# ── what the agent sends ──────────────────────────────────────────────────────

def _drive(**kwargs):
    """Run one tool call and one answer; return the provider header of each call."""
    reg = Registry()

    def get_margin(ticker: str) -> dict:
        """Get a margin.

        Args:
            ticker: the symbol.
        """
        return ok(ticker=ticker, value=0.71)

    reg.register(get_margin)
    tool_call = {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function", "function": {
                "name": "get_margin", "arguments": json.dumps({"ticker": "NVDA"})}}]}}]}
    answer = {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "The margin was 0.71."}}]}
    script = [tool_call, answer]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((body["model"], request.headers.get("x-inference-provider")))
        return httpx.Response(200, json=script.pop(0))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     base_url="http://model.invalid/v1") as client:
            return await run_agent("margin?", registry=reg, client=client, **kwargs)

    return asyncio.run(go()), calls


def test_every_call_names_its_provider_and_the_result_records_it():
    result, calls = _drive(model="big", router_model="", provider="groq")
    assert calls == [("big", "groq"), ("big", "groq")]
    assert result.provider == "groq" and result.to_dict()["provider"] == "groq"


def test_the_default_provider_comes_from_settings():
    result, calls = _drive(model="big", router_model="")
    assert {p for _, p in calls} == {"ollama"} and result.provider == "ollama"


def test_the_same_model_on_two_providers_is_still_a_router_split():
    """Same id, different providers: the router is not collapsed into the answer model."""
    result, _ = _drive(model="m", router_model="m", provider="ollama",
                       router_provider="ollama")
    assert result.router_model == ""
    # Different providers keep the split. The router call is streamed, and this
    # scripted upstream answers with plain JSON, so only the bookkeeping is checked.
    result, _ = _drive(model="m", router_model="", provider="ollama", router_provider="groq")
    assert result.router_provider == "groq"


# ── waiting out a hosted provider's rate limit ────────────────────────────────

@pytest.fixture
def limited(monkeypatch):
    """A hosted upstream that answers 429 a set number of times, then 200.

    `asyncio.sleep` in the proxy is replaced, so the waits are recorded, not slept.
    """
    state = {"refusals": 0, "calls": 0, "slept": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= state["refusals"]:
            return httpx.Response(429, json={"error": {"message": "rate limit"}},
                                  headers={"retry-after": "7"})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "ok"}}]})

    async def fake_sleep(seconds):
        state["slept"].append(seconds)

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://hosted.invalid/v1")
    monkeypatch.setattr(inference_proxy, "_client", upstream)
    monkeypatch.setattr(inference_proxy, "_hosted", {"groq": upstream})
    monkeypatch.setattr(inference_proxy.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(settings, "internal_token", "the-process-token")
    app = FastAPI()
    app.include_router(inference_proxy.router, prefix="/v1")
    return TestClient(app), state


HOSTED = {**INTERNAL, "X-Inference-Provider": "groq"}


def test_a_hosted_429_is_waited_out_for_the_time_the_provider_states(limited):
    client, state = limited
    state["refusals"] = 2
    response = client.post("/v1/chat/completions", json=BODY, headers=HOSTED)
    assert response.status_code == 200
    assert state["slept"] == [7.0, 7.0] and state["calls"] == 3
    assert response.headers["x-rate-limit-wait-ms"] == "14000"


def test_no_rate_limit_reports_no_wait(limited):
    client, state = limited
    response = client.post("/v1/chat/completions", json=BODY, headers=HOSTED)
    assert response.headers["x-rate-limit-wait-ms"] == "0" and state["slept"] == []


def test_a_limit_that_never_clears_is_returned_as_the_429_it_is(limited):
    client, state = limited
    state["refusals"] = 10**6
    response = client.post("/v1/chat/completions", json=BODY, headers=HOSTED)
    assert response.status_code == 429
    assert state["calls"] == inference_proxy.RATE_LIMIT_ATTEMPTS


def test_the_local_provider_is_never_retried(limited):
    """core/retry.py's rule for inference stands for the local server."""
    client, state = limited
    state["refusals"] = 1
    response = client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 429 and state["calls"] == 1 and state["slept"] == []


def test_one_wait_is_capped(limited, monkeypatch):
    client, state = limited
    state["refusals"] = 1
    monkeypatch.setattr(inference_proxy, "_retry_after_seconds", lambda headers: 3600.0)
    client.post("/v1/chat/completions", json=BODY, headers=HOSTED)
    assert state["slept"] == [inference_proxy.RATE_LIMIT_MAX_WAIT]


def test_the_agent_reports_the_wait_the_proxy_reported():
    reg = Registry()
    answer = {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "Nothing numeric to report."}}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answer, headers={"x-rate-limit-wait-ms": "4200"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     base_url="http://model.invalid/v1") as client:
            return await run_agent("hello", registry=reg, client=client,
                                   model="big", router_model="", provider="groq")

    result = asyncio.run(go())
    assert result.rate_limit_wait_ms == 4200.0
    assert result.to_dict()["rate_limit_wait_ms"] == 4200.0


# ── cached prompt tokens ──────────────────────────────────────────────────────

def test_the_agent_records_cached_prompt_tokens_per_call_and_in_total():
    """A hosted provider may not count cached tokens against its limits, so the
    run has to say how many there were, and on which call."""
    reg = Registry()

    def get_margin(ticker: str) -> dict:
        """Get a margin.

        Args:
            ticker: the symbol.
        """
        return ok(ticker=ticker, value=0.71)

    reg.register(get_margin)
    tool_call = {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function", "function": {
                "name": "get_margin", "arguments": json.dumps({"ticker": "NVDA"})}}]}}],
        "usage": {"prompt_tokens": 2406, "completion_tokens": 71}}
    answer = {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "The margin was 0.71."}}],
        "usage": {"prompt_tokens": 2600, "completion_tokens": 40,
                  "prompt_tokens_details": {"cached_tokens": 2304}}}
    script = [tool_call, answer]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=script.pop(0))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     base_url="http://model.invalid/v1") as client:
            return await run_agent("margin?", registry=reg, client=client,
                                   model="big", router_model="", provider="groq")

    result = asyncio.run(go())
    assert result.prompt_tokens == 5006 and result.cached_prompt_tokens == 2304
    assert result.call_usage == [
        {"prompt_tokens": 2406, "cached_prompt_tokens": 0, "completion_tokens": 71},
        {"prompt_tokens": 2600, "cached_prompt_tokens": 2304, "completion_tokens": 40},
    ]
    as_dict = result.to_dict()
    assert as_dict["cached_prompt_tokens"] == 2304 and len(as_dict["call_usage"]) == 2


def test_a_provider_that_reports_no_cache_block_records_zero():
    result, _ = _drive(model="big", router_model="", provider="ollama")
    assert result.cached_prompt_tokens == 0
