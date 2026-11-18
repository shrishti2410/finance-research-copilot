"""When the tool-free last step fails, the user gets what the tools found.

Seen in the hosted run of 2026-11-18 (benchmarks/HOSTED_RUN_2026-11-18_GUARD_BROAD.md):
the last step, sent without tools, came back 400 Bad Request, and the run ended
with "I couldn't reach the model". The likely cause, not confirmed because the
body was not logged, is the model calling a tool on a step that offered none.

Approved 2026-11-18 (option 1): if the last step fails and the tools have
established something, the answer is those findings in plain sentences, with
stop_reason "last_step_failed", and the provider's status and error body are
logged. Every figure shown comes from a tool result, as in the step-limit
message. Nothing else changes: a failure on an earlier step, or with nothing
found, is still reported as an infrastructure failure.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from agent import orchestrator
from agent.orchestrator import run_agent
from tools.registry import Registry

GROQ_400 = {"error": {"message": "Tool choice is none, but model called a tool",
                      "type": "invalid_request_error", "code": "tool_use_failed"}}


def _registry(ok=True):
    def get_financial_fact(ticker: str, fact: str, fiscal_year: str) -> dict:
        """Get one reported financial figure.

        Args:
            ticker: A ticker.
            fact: A fact name.
            fiscal_year: A fiscal year.
        """
        if not ok:
            return {"ok": False, "error": "no such fact"}
        return {"ok": True, "ticker": ticker, "fact": fact, "fiscal_year": 2026,
                "description": "Total revenue (net sales)", "value": 215938000000, "unit": "USD",
                "formatted": "$215.938 billion", "accession": "0001045810-26-000021"}
    reg = Registry()
    reg.register(get_financial_fact, name="get_financial_fact")
    return reg


class Model:
    """Calls the tool whenever tools are offered; the call numbered `fail_on`
    (1-based) gets `status` with a Groq-style error body."""

    def __init__(self, fail_on, status=400):
        self.fail_on, self.status, self.requests = fail_on, status, []

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        n = len(self.requests)
        if n == self.fail_on:
            return httpx.Response(self.status, json=GROQ_400)
        if body.get("tools"):
            msg = {"content": "", "tool_calls": [{"id": f"c{n}", "type": "function", "function": {
                "name": "get_financial_fact",
                "arguments": json.dumps({"ticker": "NVDA", "fact": "revenue", "fiscal_year": "latest"})}}]}
        else:
            msg = {"content": "NVIDIA's fiscal 2026 revenue was $215.938 billion.\nFinal figure: none"}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})


def _run(model, registry=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(model.handler),
                                     base_url="http://model.invalid/v1") as client:
            return await run_agent("What is your analysis of NVIDIA?", registry=registry or _registry(),
                                   client=client, model="fake-model", router_model="")
    return asyncio.run(go())


LAST = orchestrator.MAX_ITERATIONS


def test_a_failed_last_step_shows_the_findings_not_an_infrastructure_error():
    result = _run(Model(fail_on=LAST))
    assert result.stop_reason == "last_step_failed"
    assert not result.completed
    assert "$215.938 billion" in result.answer                 # the tool's figure, in a sentence
    assert "couldn't reach the model" not in result.answer
    assert '{"' not in result.answer and '"ok":' not in result.answer   # never raw JSON
    assert "Educational project, not investment advice." in result.answer


def test_the_providers_status_and_error_body_are_logged(caplog):
    with caplog.at_level(logging.ERROR, logger="agent.orchestrator"):
        _run(Model(fail_on=LAST))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "400" in text and "tool_use_failed" in text


@pytest.mark.parametrize("status", [400, 500, 503])
def test_any_failure_of_the_last_step_falls_back(status):
    assert _run(Model(fail_on=LAST, status=status)).stop_reason == "last_step_failed"


def test_with_nothing_found_it_is_still_an_infrastructure_failure():
    result = _run(Model(fail_on=LAST), registry=_registry(ok=False))
    assert result.stop_reason == "inference_error"
    assert "couldn't reach the model" in result.answer


def test_a_failure_before_the_last_step_is_unchanged():
    result = _run(Model(fail_on=2))
    assert result.stop_reason == "inference_error"
    assert "couldn't reach the model" in result.answer
