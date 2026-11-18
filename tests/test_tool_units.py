"""Every numeric field an agent tool returns has a declared unit, and the
grounding guard matches percentages by it (agent/field_units.py).

The outputs checked are real ones, captured from every tool on 2026-11-17
(tests/fixtures/tool_outputs.json). A tool that starts returning a numeric
field nobody has declared fails the first test here, so a percentage cannot
arrive under an unlabelled name without someone deciding what it is.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent.field_units import unit_of
from agent.grounding import unsupported_figures
from agent.orchestrator import Step

pytestmark = pytest.mark.safety

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "tool_outputs.json").read_text(encoding="utf-8"))
OUTPUTS = {o["tool"]: o["result"] for o in FIXTURE["outputs"] if o["result"].get("ok")}
_PCT = re.compile(r"pct|percent", re.I)


def _numeric_fields(value, path=""):
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _numeric_fields(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for item in value:
            yield from _numeric_fields(item, path + "[]")


def test_the_fixture_covers_every_tool_that_returns_numbers():
    assert {"get_financial_fact", "calculate_ratio", "calculate", "get_stock_price",
            "get_price_analytics", "get_trading_signal", "search_filings",
            "show_price_chart"} <= set(OUTPUTS)


@pytest.mark.parametrize("tool", sorted(OUTPUTS))
def test_every_numeric_field_has_a_declared_unit(tool):
    undeclared = [p for p, _ in _numeric_fields(OUTPUTS[tool])
                  if unit_of(tool, p) is None and not _PCT.search(p.split(".")[-1])]
    assert undeclared == [], f"{tool}: declare these in agent/field_units.py: {undeclared}"


def test_no_field_without_pct_in_its_name_holds_a_percentage_as_written():
    # The audit's finding, pinned: the rates are fractions.
    a = OUTPUTS["get_price_analytics"]
    for key in ("total_return", "annualised_return", "annualised_volatility", "max_drawdown",
                "best_day", "worst_day"):
        assert abs(a[key]) < 5, (key, a[key])
    assert abs(OUTPUTS["calculate_ratio"]["value"]) < 5


def _step(tool):
    return Step(iteration=1, tool=tool, arguments={}, result=OUTPUTS[tool],
                latency_ms=1.0, model_latency_ms=1.0, ok=True)


def _flagged(answer, *tools):
    return [f.text for f in unsupported_figures(answer, [_step(t) for t in tools])]


def _pct(x):
    return f"{x * 100:.2f}%"


def test_ratio_fields_support_their_percentage():
    a = OUTPUTS["get_price_analytics"]
    answer = f"Annualised volatility was {_pct(a['annualised_volatility'])}."
    assert _flagged(answer, "get_price_analytics") == []


@pytest.mark.parametrize("key, words", [("max_drawdown", "a maximum drawdown of"),
                                        ("total_return", "the stock fell")])
def test_a_loss_stated_without_its_sign_is_supported(key, words):
    value = OUTPUTS["get_price_analytics"][key]
    assert value < 0
    assert _flagged(f"Over the year {words} {_pct(abs(value))}.", "get_price_analytics") == []


def test_level_fields_do_not_support_a_percentage():
    rsi = OUTPUTS["show_price_chart"]["latest"]["rsi14"]
    sharpe = OUTPUTS["get_price_analytics"]["sharpe_ratio"]
    assert _flagged(f"The RSI was {rsi:.2f}%.", "show_price_chart") == [f"{rsi:.2f}%"]
    assert _flagged(f"Its return was {_pct(sharpe)}.", "get_price_analytics") == [_pct(sharpe)]


def test_percent_fields_and_the_calculator_support_their_values():
    p = OUTPUTS["get_stock_price"]["change_pct"]
    assert _flagged(f"The stock rose {p:.2f}%.", "get_stock_price") == []
    c = OUTPUTS["calculate"]["value"]
    assert _flagged(f"Gross margin was {c:.2f}%.", "calculate") == []
    r = OUTPUTS["calculate_ratio"]["value"]
    assert _flagged(f"Net margin was {_pct(r)}.", "calculate_ratio") == []


def test_a_vote_share_is_a_ratio():
    share = OUTPUTS["get_trading_signal"]["votes"][0]["share_of_vote"]
    assert _flagged(f"The top rule carries {_pct(share)} of the vote.", "get_trading_signal") == []


def test_an_undeclared_tool_keeps_the_old_reading():
    step = Step(iteration=1, tool="search_news", arguments={}, result={"ok": True, "sentiment": 0.25},
                latency_ms=1.0, model_latency_ms=1.0, ok=True)
    assert [f.text for f in unsupported_figures("Sentiment was 25%.", [step])] == []
