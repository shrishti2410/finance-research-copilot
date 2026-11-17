"""The per-request token budget (agent/budget.py) and its use in the agent loop.

Two questions in the 2026-11-17 calculator run were lost to Groq 413s
(requests estimated at 8,100 and 8,250 tokens, over the 8,000 tokens-a-minute
limit). Every request is now estimated and, over the budget, the largest tool
results are trimmed, saying what was dropped, inside the untrusted wrapper,
and only in the copy sent to the model.
"""

from __future__ import annotations

import json

import pytest

from agent.budget import (
    CHARS_PER_TOKEN,
    estimate_tokens,
    fit_to_budget,
    request_tokens,
    trim_result,
)
from agent.untrusted import wrap_tool_result


def _hits(n=3, chars=6000):
    return {"ok": True, "query": "Apple buyback program",
            "hits": [{"rank": i + 1, "section": "Item 7", "content": f"hit {i + 1} " + "x" * chars}
                     for i in range(n)]}


def _conversation(results: dict[str, dict]):
    msgs = [{"role": "system", "content": "s" * 4000},
            {"role": "user", "content": "What did Apple spend on buybacks?"}]
    table = {}
    for cid, result in results.items():
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": cid}]})
        msgs.append({"role": "tool", "tool_call_id": cid, "name": "search_filings",
                     "content": wrap_tool_result("search_filings", result)})
        table[cid] = ("search_filings", result)
    return msgs, table


TOOLS = [{"type": "function", "function": {"name": "search_filings", "parameters": {}}}]


def test_estimates_stay_on_the_high_side_of_what_groq_counted():
    # Calibrated on the 108 calls of the 2026-11-17 calculator run (agent/budget.py):
    # messages at about 4.9 characters a token, the schemas far cheaper than
    # their JSON. Both rates stay below what was observed, so the estimate is high.
    from agent.budget import TOOL_CHARS_PER_TOKEN
    assert CHARS_PER_TOKEN <= 4.9 and TOOL_CHARS_PER_TOKEN < 12875 / 2121 + 0.1
    assert estimate_tokens("x" * 35) == 10
    tools = [{"x": "y" * 588}]                      # 599 characters of JSON; "[]" is 2
    assert request_tokens([], tools) == 1 + 100


def test_a_request_within_budget_goes_out_unchanged():
    msgs, table = _conversation({"c1": _hits(1, 500)})
    sent, report = fit_to_budget(msgs, TOOLS, 7000, table, wrap_tool_result)
    assert sent is msgs and report["trimmed"] == []


def test_an_over_budget_request_is_trimmed_under_the_budget():
    msgs, table = _conversation({"c1": _hits(), "c2": _hits(), "c3": _hits()})
    assert request_tokens(msgs, TOOLS) > 7000                      # the 413 shape
    sent, report = fit_to_budget(msgs, TOOLS, 7000, table, wrap_tool_result)
    assert request_tokens(sent, TOOLS) <= 7000
    assert report["estimated_tokens"] <= 7000 and not report["over_budget"]
    assert report["trimmed"]


def test_a_trimmed_result_says_what_was_left_out_and_keeps_the_best_hits():
    trimmed = trim_result(_hits(5, 3000), 7000)
    assert len(json.dumps(trimmed)) <= 7600
    assert "_trimmed" in trimmed and "of 5 hits" in trimmed["_trimmed"]
    assert trimmed["hits"][0]["rank"] == 1                        # ranked: best first


def test_trimming_happens_inside_the_untrusted_wrapper():
    msgs, table = _conversation({"c1": _hits(), "c2": _hits(), "c3": _hits()})
    sent, _ = fit_to_budget(msgs, TOOLS, 7000, table, wrap_tool_result)
    for m in sent:
        if m.get("role") == "tool":
            # Every tool message is a whole wrapped result: re-wrapping what it
            # carries gives the same text, so the closing delimiter is intact.
            body = m["content"]
            start, end = wrap_tool_result("search_filings", {"x": 1}).split('{"x": 1}')
            assert body.startswith(start.split("\n")[0]) and body.rstrip().endswith(end.strip())


def test_the_original_messages_and_results_are_not_changed():
    msgs, table = _conversation({"c1": _hits(), "c2": _hits(), "c3": _hits()})
    before = json.dumps(msgs)
    originals = json.dumps(table)
    fit_to_budget(msgs, TOOLS, 7000, table, wrap_tool_result)
    assert json.dumps(msgs) == before and json.dumps(table) == originals


def test_errors_and_non_tool_messages_are_never_trimmed():
    err = {"ok": False, "error": "e" * 20000}
    assert trim_result(err, 100) is err
    msgs, table = _conversation({"c1": _hits()})
    msgs[1]["content"] = "q" * 40000                              # a huge question
    sent, report = fit_to_budget(msgs, TOOLS, 7000, table, wrap_tool_result)
    assert sent[1]["content"] == msgs[1]["content"]
    assert report["over_budget"]                                  # logged, not hidden


def test_a_budget_of_zero_turns_it_off():
    msgs, table = _conversation({"c1": _hits(), "c2": _hits(), "c3": _hits()})
    sent, report = fit_to_budget(msgs, TOOLS, 0, table, wrap_tool_result)
    assert sent is msgs and report["trimmed"] == []


def test_the_default_budget_is_under_groqs_limit():
    from core.config import Settings
    budget = Settings(_env_file=None).agent_request_token_budget
    assert 0 < budget <= 7000


# --- in the agent loop --------------------------------------------------------------

def test_the_agent_loop_keeps_every_request_under_the_budget(monkeypatch):
    """Three large tool results, then an answer: the shape that drew the 413s.
    Every request the model receives is within the budget; the trace records
    each request's size; the steps keep the full results for the guard.

    The tool is not search_filings, whose results compact_for_model already
    shortens: a tool it passes through whole is the case the budget is for."""
    import asyncio

    import httpx

    from agent import orchestrator
    from agent.tools import Registry

    big = _hits(3, 6000)

    def read_filing_section(query: str, company: str = "") -> dict:
        """Read a section of a 10-K filing.

        Args:
            query: what to look for.
            company: a ticker.
        """
        return big

    reg = Registry()
    reg.register(read_filing_section, name="read_filing_section")
    sizes: list[int] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sizes.append(request_tokens(body["messages"], body["tools"]))
        calls["n"] += 1
        if calls["n"] <= 3:
            msg = {"content": "", "tool_calls": [{
                "id": f"c{calls['n']}", "type": "function",
                "function": {"name": "read_filing_section",
                             "arguments": json.dumps({"query": f"buyback {calls['n']}", "company": "AAPL"})}}]}
        else:
            msg = {"content": "Apple's filing describes its share repurchase program."}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})

    monkeypatch.setattr(orchestrator.settings, "agent_request_token_budget", 7000)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     base_url="http://model.invalid/v1") as client:
            return await orchestrator.run_agent("What did Apple spend on buybacks?", registry=reg,
                                                client=client, model="fake-model", router_model="")

    result = asyncio.run(go())
    assert calls["n"] >= 4
    assert max(sizes) <= 7000, sizes
    assert [r["estimated_tokens"] for r in result.request_sizes] == sizes
    assert result.to_dict()["request_sizes"] == result.request_sizes
    assert any(r["trimmed"] for r in result.request_sizes)
    searches = [s for s in result.steps if s.tool == "read_filing_section"]
    assert len(searches) == 3 and all(len(s.result["hits"]) == 3 for s in searches)
