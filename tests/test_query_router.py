"""The query router: a news, price or reported-figure question about one
covered company goes straight to its tool, then one short model call writes
the answer (live-site feedback, 2026-11-18: news questions hit ReadTimeouts
after several model round trips). Everything else goes to the agent as before.

The routed answer is written on a step with no tools, the same final step as
the agent's, so the grounding check, the [Unverified] marks and the advice
check all apply to it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import httpx
import pytest

from agent.orchestrator import run_agent
from agent.router import route
from tools.registry import Registry

TODAY = date(2026, 11, 18)


# --- what is routed -------------------------------------------------------------

@pytest.mark.parametrize("question, tool, args", [
    ("What is the latest news about Dixon?", "search_news", {"query": "Dixon Technologies (India) Limited", "days_back": 7}),
    ("Any news on NVIDIA this week?", "search_news", {"query": "NVIDIA Corporation", "days_back": 7}),
    ("What did HDFC Bank announce recently?", "search_news", {"query": "HDFC Bank Limited", "days_back": 7}),
    ("What is SBIN.NS trading at?", "get_stock_price", {"ticker": "SBIN.NS", "start_date": "2026-11-04"}),
    ("What's Apple's share price?", "get_stock_price", {"ticker": "AAPL", "start_date": "2026-11-04"}),
    ("What was NVIDIA's revenue in fiscal 2026?", "get_financial_fact",
     {"ticker": "NVDA", "fact": "revenue", "fiscal_year": 2026}),
    ("What was Apple's net income in FY2025?", "get_financial_fact",
     {"ticker": "AAPL", "fact": "net_income", "fiscal_year": 2025}),
    ("What is Microsoft's diluted EPS?", "get_financial_fact",
     {"ticker": "MSFT", "fact": "eps_diluted", "fiscal_year": "latest"}),
])
def test_a_plain_question_about_one_company_is_routed(question, tool, args):
    assert route(question, today=TODAY) == (tool, args)


@pytest.mark.parametrize("question", [
    "What is your analysis of NVIDIA?",                 # broad: the agent and its snapshot
    "Compare NVIDIA and Apple revenue in fiscal 2026.", # two companies
    "How has Dixon performed over the last year?",      # analytics, not a lookup
    "Will Apple's price go up next week?",              # prediction: the agent declines it
    "What was Dixon's revenue in fiscal 2026?",         # no 10-K: facts are for the 15 US companies
    "What did NVIDIA close at on 2026-08-07?",          # a dated price: the agent handles dates
    "What is the latest news on interest rates?",       # no covered company
    "What was NVIDIA's revenue growth in fiscal 2026?", # a derived figure: the calculator's job
])
def test_anything_else_goes_to_the_agent(question):
    assert route(question, today=TODAY) is None


# --- the routed run --------------------------------------------------------------

def _registry(calls):
    def search_news(query: str, days_back: int = 7) -> dict:
        """Search news.

        Args:
            query: What to search for.
            days_back: Days back.
        """
        calls.append(("search_news", query))
        return {"ok": True, "query": query, "count": 1, "filtered_out": 2,
                "articles": [{"title": "Dixon Technologies to Acquire Vimal Plast Assets",
                              "source": "MarketScreener", "published": "2026-11-17T08:00:00+00:00",
                              "url": "https://ex.com/v", "snippet": ""}]}
    reg = Registry()
    reg.register(search_news)
    return reg


class Model:
    def __init__(self, fail=0, exc=httpx.ReadTimeout):
        self.requests, self.fail, self.exc = [], fail, exc

    def handler(self, request):
        self.requests.append(json.loads(request.content))
        if len(self.requests) <= self.fail:
            raise self.exc("timed out", request=request)
        msg = {"content": "Dixon Technologies agreed to buy Vimal Plast's assets, per MarketScreener "
                          "on 17 Nov 2026.\nFinal figure: none"}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})


def _run(model, question="What is the latest news about Dixon?", history=None, calls=None):
    calls = [] if calls is None else calls

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(model.handler),
                                     base_url="http://model.invalid/v1") as client:
            return await run_agent(question, history, registry=_registry(calls), client=client,
                                   model="fake-model", router_model="", today=TODAY)
    return asyncio.run(go())


def test_a_routed_question_makes_one_model_call_with_no_tools():
    model, calls = Model(), []
    result = _run(model, calls=calls)
    assert calls == [("search_news", "Dixon Technologies (India) Limited")]
    assert len(model.requests) == 1 and "tools" not in model.requests[0]
    assert result.completed and "Vimal Plast" in result.answer
    assert [s.tool for s in result.steps][0] == "search_news"


def test_a_follow_up_in_a_conversation_is_not_routed():
    model, calls = Model(), []
    _run(model, history=[{"role": "user", "content": "Tell me about Dixon."},
                         {"role": "assistant", "content": "Dixon Technologies is ..."}], calls=calls)
    assert "tools" in model.requests[0]           # the agent chose; nothing was pre-called
    assert calls == []


# --- timeouts: one retry with backoff, then a friendly message --------------------

def test_a_timed_out_model_call_is_retried_once(monkeypatch):
    monkeypatch.setattr("agent.orchestrator.MODEL_RETRY_BACKOFF_S", 0.0)
    model = Model(fail=1)
    result = _run(model)
    assert len(model.requests) == 2 and result.completed


def test_two_timeouts_end_with_the_busy_message_and_are_logged(monkeypatch, caplog):
    import logging
    monkeypatch.setattr("agent.orchestrator.MODEL_RETRY_BACKOFF_S", 0.0)
    model = Model(fail=5)
    with caplog.at_level(logging.WARNING, logger="agent.orchestrator"):
        result = _run(model, question="Tell me something about the market.")
    assert result.stop_reason == "model_busy"
    assert "AI is busy" in result.answer and "try again in a minute" in result.answer
    assert "ReadTimeout" in " ".join(r.getMessage() for r in caplog.records)
    assert len(model.requests) == 2               # one retry, not more


def test_a_route_to_a_tool_the_registry_lacks_goes_to_the_agent():
    model, calls = Model(), []
    _run(model, question="What is SBIN.NS trading at?", calls=calls)   # registry has news only
    assert "tools" in model.requests[0] and calls == []
