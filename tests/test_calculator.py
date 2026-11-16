"""The calculator tool, and the guard rule that goes with it.

Three things are pinned here:

- the tool computes, from numbers and from fact references, and returns its
  inputs;
- an operand no tool returned is refused before the tool runs, so an invented
  number cannot be turned into evidence by passing it through `calculate`;
- an answer's figure has to be in a tool result: arithmetic the model did
  itself is flagged however correct, and the same figure is accepted once the
  calculator has returned it.

Nothing here calls a model. The loop tests script the model with a
MockTransport.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent.grounding import ungrounded_operands, unsupported_figures
from agent.orchestrator import Step, run_agent
from tools import calculator
from tools.base import ok
from tools.calculator import calculate, is_reference, parse_operand
from tools.registry import Registry

pytestmark = pytest.mark.safety


def run(coro):
    return asyncio.run(coro)


def _ok(tool: str, result, iteration: int = 1) -> Step:
    return Step(iteration=iteration, tool=tool, arguments={}, result=result,
                latency_ms=1.0, model_latency_ms=1.0, ok=True)


def _texts(figures) -> list[str]:
    return [f.text for f in figures]


# ── the tool ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("operation, a, b, value", [
    ("add", 2, 3, 5),
    ("subtract", 0.710681, 0.487856, 0.222825),
    ("multiply", 24514000000, 223.7097, 5484019585800.0),
    ("divide", 223.7097, 4.9, 45.655041),
    ("percent_change", 130497000000, 215938000000, 65.473536),
    ("percent_of", 109158000000, 416161000000, 26.229752),
])
def test_each_operation_computes(operation, a, b, value):
    result = run(calculate(operation, a, b))
    assert result["ok"] and result["operation"] == operation
    assert result["value"] == pytest.approx(value, rel=1e-9)


def test_the_result_carries_its_inputs_its_formula_and_roundings():
    result = run(calculate("percent_change", 130497000000, 215938000000,
                           a_label="NVDA FY2025 revenue", b_label="NVDA FY2026 revenue"))
    assert result["formatted"] == "65.47%" and result["unit"] == "percent"
    assert result["inputs"]["a"] == {"label": "NVDA FY2025 revenue", "value": 130497000000.0,
                                     "source": "a tool result from this turn"}
    assert "215,938,000,000 - 130,497,000,000" in result["formula"]
    assert result["rounded"] == {"2dp": "65.47%", "1dp": "65.5%", "0dp": "65%"}


def test_a_large_amount_is_also_given_in_the_scales_an_answer_uses():
    rounded = run(calculate("multiply", 24514000000, 223.7097))["rounded"]
    assert rounded["2dp_trillion"] == "5.48 trillion"
    assert rounded["2dp_billion"] == "5,484.02 billion"
    assert not any(text.startswith("0") for text in rounded.values())


def test_a_rounding_that_keeps_one_digit_is_not_offered():
    rounded = run(calculate("divide", 7, 100))["rounded"]          # 0.07
    assert "0dp" not in rounded and "1dp" not in rounded


@pytest.mark.parametrize("spelling", ["growth", "percentage change", "pct_change"])
def test_common_names_for_an_operation_are_accepted(spelling):
    assert run(calculate(spelling, 100, 150))["operation"] == "percent_change"


def test_an_unknown_operation_lists_the_known_ones():
    result = run(calculate("power", 2, 3))
    assert result["error"] == "unsupported" and "percent_change" in result["message"]


@pytest.mark.parametrize("operation, a, b", [
    ("divide", 1, 0), ("percent_of", 1, 0), ("percent_change", 0, 5),
])
def test_division_by_zero_is_an_error_not_an_exception(operation, a, b):
    result = run(calculate(operation, a, b))
    assert not result["ok"] and "undefined" in result["message"]


@pytest.mark.parametrize("raw, value", [
    (215938000000, 215938000000.0), ("215,938", 215938.0), ("$4.90", 4.9),
    ("71.07%", 71.07), ("215.9 billion", 215.9e9), ("-5.53", -5.53), ("1e3", 1000.0),
])
def test_an_operand_is_read_as_a_model_writes_it(raw, value):
    assert parse_operand(raw) == pytest.approx(value)


@pytest.mark.parametrize("raw", ["revenue", "", None, True, "about 5", "5 apples"])
def test_what_is_not_a_number_is_not_guessed_at(raw):
    assert parse_operand(raw) is None
    if raw not in (None, True):
        assert run(calculate("add", raw, 1))["error"] == "bad_input"


def test_a_fact_reference_is_recognised():
    assert is_reference("NVDA:revenue:2026") and is_reference("aapl:net_income:FY2025")
    assert not is_reference("215938000000") and not is_reference(2026)


def test_a_fact_reference_is_looked_up_and_cited(monkeypatch):
    asked = []

    async def fake_fact(ticker, fact, fiscal_year):
        asked.append((ticker, fact, fiscal_year))
        return ok(ticker=ticker.upper(), fact=fact, fiscal_year=fiscal_year, unit="USD",
                  value={2025: 130497000000, 2026: 215938000000}[fiscal_year],
                  accession=f"0001045810-{fiscal_year - 2000}-000021",
                  period_end=f"{fiscal_year}-01-25")

    monkeypatch.setattr(calculator, "get_financial_fact", fake_fact)
    result = run(calculate("percent_change", "NVDA:revenue:2025", "NVDA:revenue:FY2026"))
    assert asked == [("NVDA", "revenue", 2025), ("NVDA", "revenue", 2026)]
    assert result["formatted"] == "65.47%"
    assert result["inputs"]["b"]["accession"] == "0001045810-26-000021"
    assert result["inputs"]["a"]["label"] == "NVDA fiscal 2025 revenue"
    assert result["inputs"]["a"]["source"] == "get_financial_fact"


def test_a_reference_that_cannot_be_looked_up_is_the_lookups_error(monkeypatch):
    async def fake_fact(ticker, fact, fiscal_year):
        return {"ok": False, "error": "no_data", "message": "NVDA has no revenue for 1999."}

    monkeypatch.setattr(calculator, "get_financial_fact", fake_fact)
    result = run(calculate("add", "NVDA:revenue:1999", 1))
    assert result["error"] == "no_data" and "1999" in result["message"]


def test_a_reference_to_a_ratio_is_refused(monkeypatch):
    async def fake_fact(ticker, fact, fiscal_year):
        return ok(ticker="NVDA", ratio="gross_margin", value=0.710681, fiscal_year=2026)

    monkeypatch.setattr(calculator, "get_financial_fact", fake_fact)
    result = run(calculate("multiply", "NVDA:gross_margin:2026", 2))
    assert result["error"] == "bad_input" and "calculate_ratio" in result["message"]


def test_the_tool_is_registered_and_its_description_says_never_compute():
    from agent.tools import build_registry
    spec = build_registry().get("calculate")
    description = " ".join(spec.schema()["function"]["description"].split())
    assert "Never do arithmetic yourself" in description
    assert spec.parameters["properties"]["operation"]["enum"] == list(calculator.OPERATIONS)
    assert spec.required == {"operation", "a", "b"}


def test_the_system_prompt_routes_every_derived_figure_to_the_tool():
    from agent.prompts import SYSTEM_PROMPT
    text = " ".join(SYSTEM_PROMPT.split())
    assert "Never do arithmetic yourself" in text and "calculate tool" in text


# ── an operand has to be in a tool result ─────────────────────────────────────

PRICE = {"ok": True, "ticker": "NVDA", "last": {"date": "2026-08-07", "close": 223.7097}}
SHARES = {"ok": True, "fact": "shares_diluted", "value": 24514000000, "unit": "shares"}
MDNA = {"ok": True, "count": 1, "results": [{
    "section": "Item 7 - MD&A", "score": 0.81,
    "excerpt": "Compensation rose 29% while headcount grew 7% over the year."}]}


def _missing(steps, **operands):
    return ungrounded_operands({name: (raw, parse_operand(raw))
                                for name, raw in operands.items()}, steps)


def test_operands_that_tools_returned_are_grounded():
    steps = [_ok("get_stock_price", PRICE), _ok("get_financial_fact", SHARES)]
    assert _missing(steps, a=24514000000, b=223.7097) == []


def test_an_operand_as_the_model_rounds_or_rescales_it_is_grounded():
    steps = [_ok("get_stock_price", PRICE), _ok("get_financial_fact", SHARES)]
    assert _missing(steps, a="24,514 million", b=223.71) == []
    assert _missing(steps, a="24.514 billion", b="$223.71") == []


def test_an_invented_operand_is_named():
    """The Milestone 8 fabrication, routed through the calculator: "assume 1.8
    billion shares" times a real price must not become a grounded market value."""
    steps = [_ok("get_stock_price", PRICE)]
    assert _missing(steps, a=1800000000, b=223.7097) == ["a"]
    assert _missing(steps, a=1800000000, b=66.7) == ["a", "b"]


def test_an_operand_close_to_a_real_figure_but_not_a_rounding_of_it_is_refused():
    steps = [_ok("get_stock_price", PRICE)]
    assert _missing(steps, a=223.9) == ["a"]            # within 0.5%, not a rounding
    assert _missing(steps, a=225) == ["a"]
    assert _missing(steps, a=223.7) == [] and _missing(steps, a=224) == []


def test_an_operand_written_to_one_or_two_digits_must_be_a_figure_exactly():
    """"5" rounds everything from 4.5 to 5.5 in nine scales."""
    steps = [_ok("get_financial_fact", {"ok": True, "value": 4.9, "unit": "USD/shares"})]
    assert _missing(steps, a=4.9) == []
    assert _missing(steps, a=5) == ["a"] and _missing(steps, a="5.0") == ["a"]


def test_a_bare_integer_in_prose_and_a_retrieval_score_are_not_operands():
    """The 21.93% regression's operands: "Item 7" and a score are not figures."""
    steps = [_ok("search_filings", MDNA)]
    assert _missing(steps, a=29, b=7) == []             # "29%" and "7%" are figures
    assert _missing(steps, a=0.81) == ["a"]
    steps = [_ok("search_filings", {"ok": True, "results": [{
        "section": "Item 7 - MD&A", "excerpt": "See Note 13 on page 29."}]})]
    assert _missing(steps, a=7, b=13) == ["a", "b"]


def test_nothing_is_grounded_by_a_failed_call_or_by_no_calls():
    failed = Step(iteration=1, tool="get_stock_price", arguments={}, ok=False,
                  result={"ok": False, "error": "upstream", "message": "close 223.7097"},
                  latency_ms=1.0, model_latency_ms=1.0)
    assert _missing([failed], a=223.7097) == ["a"]
    assert _missing([], a=1) == ["a"]


def test_an_earlier_result_of_the_calculator_is_an_operand_for_the_next_step():
    first = run(calculate("multiply", 24514000000, 223.7097))
    steps = [_ok("get_stock_price", PRICE), _ok("get_financial_fact", SHARES),
             _ok("calculate", first)]
    assert _missing(steps, a=first["value"]) == []


# ── an answer's figure has to be in a tool result ─────────────────────────────

def test_correct_arithmetic_done_by_the_model_is_flagged():
    steps = [_ok("get_stock_price", PRICE), _ok("get_financial_fact", SHARES)]
    answer = ("With 24,514 million diluted shares and a close of $223.71, the "
              "implied market capitalisation is about $5.48 trillion.")
    assert _texts(unsupported_figures(answer, steps)) == ["$5.48 trillion"]


def test_the_same_figure_is_accepted_once_the_calculator_returned_it():
    steps = [_ok("get_stock_price", PRICE), _ok("get_financial_fact", SHARES),
             _ok("calculate", run(calculate("multiply", 24514000000, 223.7097)))]
    for written in ("$5.48 trillion", "$5,484.02 billion", "$5.5 trillion"):
        answer = (f"With 24,514 million diluted shares and a close of $223.71, the "
                  f"implied market capitalisation is {written}.")
        assert _texts(unsupported_figures(answer, steps)) == [], written


def test_a_figure_that_is_not_what_the_calculator_returned_is_still_flagged():
    """The Qwen run's 65.51% for 65.47% is within 0.5% and passes as the same
    number; 66.7% is not, and neither is anything the tool did not return."""
    steps = [_ok("calculate", run(calculate("percent_change", 130497000000, 215938000000)))]
    assert _texts(unsupported_figures("Revenue grew 65.47%.", steps)) == []
    assert _texts(unsupported_figures("Revenue grew about 65%.", steps)) == []
    assert _texts(unsupported_figures("Revenue grew 66.7%.", steps)) == ["66.7%"]
    assert _texts(unsupported_figures("Revenue grew 56.47%.", steps)) == ["56.47%"]


def test_three_invented_and_consistent_figures_are_all_flagged():
    steps = [_ok("get_stock_price", PRICE)]
    answer = "Revenue was $100 million, costs were $60 million and profit was $40 million."
    assert len(unsupported_figures(answer, steps)) == 3


def test_with_no_tool_results_every_figure_is_flagged():
    assert _texts(unsupported_figures("The margin was 71.07%.", [])) == ["71.07%"]


# ── through the loop ──────────────────────────────────────────────────────────

def _call(name, arguments, call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def _drive(turns):
    """Run the agent on a scripted model. `turns` is a list of either a list of
    tool calls or a final answer string."""
    reg = Registry()

    def get_price(ticker: str) -> dict:
        """Get a price.

        Args:
            ticker: the symbol.
        """
        return ok(ticker=ticker, last={"date": "2026-08-07", "close": 223.7097})

    def get_shares(ticker: str) -> dict:
        """Get a share count.

        Args:
            ticker: the symbol.
        """
        return ok(ticker=ticker, fact="shares_diluted", value=24514000000, unit="shares")

    reg.register(get_price)
    reg.register(get_shares)
    reg.register(calculate)
    script = []
    for turn in turns:
        if isinstance(turn, str):
            script.append({"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": turn}}]})
        else:
            script.append({"choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "", "tool_calls": turn}}]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=script.pop(0))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     base_url="http://model.invalid/v1") as client:
            return await run_agent("NVIDIA's implied market cap?", registry=reg, client=client,
                                   model="fake-model", router_model="")
    return asyncio.run(go())


FETCH = [_call("get_price", {"ticker": "NVDA"}, "c1"), _call("get_shares", {"ticker": "NVDA"}, "c2")]


def test_fetch_then_calculate_then_answer_is_a_clean_answer():
    result = _drive([
        FETCH,
        [_call("calculate", {"operation": "multiply", "a": 24514000000, "b": 223.7097,
                             "a_label": "diluted shares", "b_label": "close"})],
        "At $223.71 a share on 24,514 million diluted shares, the implied market "
        "capitalisation is $5.48 trillion.\nFinal figure: $5.48 trillion",
    ])
    assert result.completed and result.stop_reason == "final_answer"
    assert result.final_figure == "$5.48 trillion"
    assert [s.tool for s in result.steps] == ["get_price", "get_shares", "calculate",
                                              "final_answer"]


def test_the_models_own_arithmetic_is_discarded_and_the_retry_names_the_calculator():
    own = ("At $223.71 a share on 24,514 million diluted shares, the implied market "
           "capitalisation is $5.48 trillion.")
    result = _drive([
        FETCH, own,
        [_call("calculate", {"operation": "multiply", "a": 24514000000, "b": 223.7097})],
        own,
    ])
    tools = [s.tool for s in result.steps]
    assert tools == ["get_price", "get_shares", "ungrounded_answer", "calculate", "final_answer"]
    assert result.completed, "the same sentence is accepted once the tool has returned the figure"
    from agent.orchestrator import UNGROUNDED_RETRY
    assert "calculate tool" in UNGROUNDED_RETRY


def test_an_invented_operand_never_reaches_the_tool_and_its_product_is_never_evidence():
    invented = "Assuming 1.8 billion shares, the market capitalisation is $402.68 billion."
    result = _drive([
        FETCH,
        [_call("calculate", {"operation": "multiply", "a": 1800000000, "b": 223.7097})],
        invented, invented,
    ])
    refused = next(s for s in result.steps if s.tool == "calculate")
    assert refused.ok is False and refused.result["error"] == "bad_input"
    assert "Operand a (1800000000)" in refused.result["message"]
    assert "value" not in refused.result
    assert result.stop_reason == "unverified_figures" and not result.completed
    assert result.answer.startswith("[Unverified:") and "$402.68 billion" in result.answer


def test_operands_fetched_in_the_same_batch_as_the_calculation_are_not_yet_evidence():
    """A model that sends the lookups and the calculation together is guessing
    the numbers. Calls run in order, so the lookups land first and a correct
    guess is grounded; a wrong one is refused."""
    result = _drive([
        FETCH + [_call("calculate", {"operation": "multiply", "a": 24000000000,
                                     "b": 223.7097}, "c3")],
        "I could not establish the figure.\nFinal figure: none",
    ])
    refused = next(s for s in result.steps if s.tool == "calculate")
    assert refused.ok is False and "Operand a" in refused.result["message"]
