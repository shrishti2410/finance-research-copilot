"""Broad questions: one snapshot call, a last step kept for the answer, and never
raw tool output in front of the user.

Found on the live site, 2026-11-18: "What is your analysis of NVIDIA?" spent all
five steps calling get_financial_fact once each, then showed the user the
exhaustion message with every tool result pasted in as raw JSON.
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from agent import orchestrator
from agent.orchestrator import Step, _findings, run_agent
from agent.prompts import system_prompt
from tools.registry import Registry
from tools.snapshot import _ratios


# --- the loop keeps its last step for the answer --------------------------------------

def _fact_tool():
    def get_financial_fact(ticker: str, fact: str, fiscal_year: str) -> dict:
        """Get one reported financial figure.

        Args:
            ticker: A ticker.
            fact: A fact name.
            fiscal_year: A fiscal year.
        """
        return {"ok": True, "ticker": ticker, "fact": fact, "fiscal_year": 2026,
                "description": "Total revenue (net sales)", "value": 215938000000, "unit": "USD",
                "formatted": "$215.938 billion", "accession": "0001045810-26-000021"}
    reg = Registry()
    reg.register(get_financial_fact, name="get_financial_fact")
    return reg


class Model:
    """Asks for a tool whenever it is offered tools; answers when it is not
    (or, with `stubborn`, keeps asking for tools regardless)."""

    def __init__(self, stubborn=False):
        self.requests = []
        self.stubborn = stubborn

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        n = len(self.requests)
        if body.get("tools") or self.stubborn:
            msg = {"content": "", "tool_calls": [{"id": f"c{n}", "type": "function", "function": {
                "name": "get_financial_fact",
                "arguments": json.dumps({"ticker": "NVDA", "fact": "revenue", "fiscal_year": "latest"})}}]}
        else:
            msg = {"content": "NVIDIA's fiscal 2026 revenue was $215.938 billion.\nFinal figure: $215.938 billion"}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})


def _run(model, question="What is your analysis of NVIDIA?"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(model.handler),
                                     base_url="http://model.invalid/v1") as client:
            return await run_agent(question, registry=_fact_tool(), client=client,
                                   model="fake-model", router_model="")
    return asyncio.run(go())


def test_the_last_step_is_sent_without_tools_so_the_model_answers():
    model = Model()
    result = _run(model)
    assert len(model.requests) == orchestrator.MAX_ITERATIONS
    assert all(r.get("tools") for r in model.requests[:-1])
    assert "tools" not in model.requests[-1]
    assert result.completed and result.stop_reason == "final_answer"
    assert "$215.938 billion" in result.answer


def test_a_model_that_still_asks_for_tools_gets_plain_sentences_not_json():
    result = _run(Model(stubborn=True))
    assert result.stop_reason == "max_iterations" and not result.completed
    assert "$215.938 billion" in result.answer                     # the finding, stated
    assert "{" not in result.answer and '"ok"' not in result.answer
    assert "get_financial_fact(" not in result.answer


# --- findings are written as sentences --------------------------------------------------

def _step(tool, result, arguments=None):
    return Step(iteration=1, tool=tool, arguments=arguments or {}, result=result,
                latency_ms=1.0, model_latency_ms=1.0, ok=True)


@pytest.mark.parametrize("tool, result, expect", [
    ("get_financial_fact", {"ok": True, "ticker": "NVDA", "fiscal_year": 2026, "description": "Total revenue (net sales)",
                            "formatted": "$215.938 billion", "accession": "0001045810-26-000021"},
     "NVDA total revenue (net sales) for fiscal 2026: $215.938 billion"),
    ("calculate_ratio", {"ok": True, "ticker": "AAPL", "ratio": "net_margin", "period_end": "2026-09-30",
                         "formatted": "27.60%"}, "AAPL net margin (period ending 2026-09-30): 27.60%"),
    ("get_price_analytics", {"ok": True, "ticker": "NVDA", "start": "2025-11-17", "end": "2026-11-17",
                             "total_return": -0.126404, "max_drawdown": -0.319303},
     "NVDA from 2025-11-17 to 2026-11-17: total return -12.64%, largest fall from a peak -31.93%"),
    ("get_trading_signal", {"ok": True, "ticker": "SBIN.NS", "signal": "BUY", "coverage": "12 of 15 rules",
                            "backtest": {"statement": "The backtest shows no edge for SBIN.NS."}},
     "The signal engine's output for SBIN.NS is BUY (12 of 15 rules). The backtest shows no edge"),
])
def test_known_tools_are_described_in_a_sentence(tool, result, expect):
    text = _findings([_step(tool, result)])
    assert expect in text and "{" not in text


def test_an_unknown_tool_is_named_without_its_payload():
    text = _findings([_step("mystery_tool", {"ok": True, "secret_blob": {"a": [1, 2, 3]}})])
    assert "mystery_tool" in text and "secret_blob" not in text and "{" not in text


def test_failed_steps_and_the_final_answer_are_not_findings():
    failed = Step(iteration=1, tool="get_financial_fact", arguments={}, result={"ok": False, "message": "x"},
                  latency_ms=1.0, model_latency_ms=1.0, ok=False)
    assert _findings([failed]) == ""


# --- the snapshot ----------------------------------------------------------------------

def test_margins_and_growth_are_computed_as_fractions():
    years = _ratios([{"revenue": 100.0, "gross_profit": 60.0, "operating_income": 30.0, "net_income": 20.0},
                     {"revenue": 125.0, "gross_profit": 80.0, "operating_income": 40.0, "net_income": 30.0}])
    assert years[0]["gross_margin"] == 0.6 and years[0]["net_margin"] == 0.2
    assert "revenue_growth" not in years[0]
    assert years[1]["revenue_growth"] == 0.25 and years[1]["net_income_growth"] == 0.5


def test_growth_is_not_computed_from_a_loss():
    years = _ratios([{"revenue": 100.0, "net_income": -5.0}, {"revenue": 110.0, "net_income": 10.0}])
    assert "net_income_growth" not in years[1] and years[1]["revenue_growth"] == pytest.approx(0.1)


def test_one_failing_part_does_not_lose_the_others(monkeypatch):
    from tools import snapshot

    async def boom(symbol):
        raise ConnectionError("Yahoo unreachable")

    async def perf(symbol):
        return {"total_return": 0.1}

    async def sig(symbol):
        return {"signal": "HOLD"}

    monkeypatch.setattr(snapshot, "_financials", boom)
    monkeypatch.setattr(snapshot, "_performance", perf)
    monkeypatch.setattr(snapshot, "_signal", sig)
    out = asyncio.run(snapshot.get_company_snapshot("dixon.ns"))
    assert out["ok"] and out["ticker"] == "DIXON.NS"
    assert "Yahoo unreachable" in out["financials"]["missing"]
    assert out["performance"] == {"total_return": 0.1} and out["signal"] == {"signal": "HOLD"}


def test_a_us_company_gets_three_years_from_its_filings():
    """Against the local database's XBRL facts; skipped without them."""
    from tools import snapshot

    async def go():
        from db.base import engine
        try:
            return await snapshot._sec_financials("NVDA")
        finally:
            await engine.dispose()
    try:
        fin = asyncio.run(go())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"facts not available ({type(exc).__name__})")
    if fin is None:
        pytest.skip("no NVDA facts ingested")
    assert fin["source"].startswith("SEC") and len(fin["years"]) == 3
    latest = fin["years"][-1]
    assert latest["gross_margin"] == pytest.approx(latest["gross_profit"] / latest["revenue"], abs=1e-6)
    assert re.match(r"\d{10}-\d{2}-\d{6}", latest["accession"])
    assert "revenue_growth" in fin["years"][0]          # the extra year fed the first growth


def test_an_nse_company_gets_labelled_yahoo_figures(monkeypatch):
    from tools import snapshot

    async def none(symbol):
        return None

    monkeypatch.setattr(snapshot, "_sec_financials", none)
    monkeypatch.setattr(snapshot, "_yahoo_financials", lambda s: {"source": snapshot.YAHOO_LABEL,
                                                                  "currency": "INR", "years": []})
    fin = asyncio.run(snapshot._financials("DIXON.NS"))
    assert "not a filing" in fin["source"]


# --- the prompt and the registry -------------------------------------------------------

def test_the_prompt_sends_broad_questions_to_the_snapshot_and_asks_for_sections():
    p = system_prompt(5)
    assert "get_company_snapshot" in p
    for section in ("Financials", "Growth", "Profitability", "Stock performance", "Signal", "Key risks"):
        assert section in p
    assert "last turn" in p and "no tools" in p


def test_the_snapshot_is_a_registered_tool():
    from agent.tools import build_registry
    assert "get_company_snapshot" in build_registry()
