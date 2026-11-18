"""get_company_snapshot's numeric fields have declared units, and the grounding
guard reads them by unit (agent/field_units.py).

The snapshot is what a broad question ("What is your analysis of NVIDIA?")
is answered from, so its margins and growth rates are the percentages most
answers state. They are fractions (0.710681 is 71.07%). Its amounts and EPS
are money: a diluted EPS of 4.9 must not support "4.9%". The captures, one
from SEC XBRL (NVDA) and one from Yahoo Finance (DIXON.NS), are real outputs
in tests/fixtures/tool_outputs.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.field_units import unit_of
from agent.grounding import unsupported_figures
from agent.orchestrator import Step

pytestmark = pytest.mark.safety

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "tool_outputs.json").read_text(encoding="utf-8"))
SNAPSHOTS = {o["arguments"]["ticker"]: o["result"] for o in FIXTURE["outputs"]
             if o["tool"] == "get_company_snapshot"}


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


def _flagged(answer, ticker):
    step = Step(iteration=1, tool="get_company_snapshot", arguments={"ticker": ticker},
                result=SNAPSHOTS[ticker], latency_ms=1.0, model_latency_ms=1.0, ok=True)
    return [f.text for f in unsupported_figures(answer, [step])]


def _latest(ticker):
    return SNAPSHOTS[ticker]["financials"]["years"][-1]


def test_both_sources_are_captured():
    assert set(SNAPSHOTS) == {"NVDA", "DIXON.NS"}
    assert all(s["ok"] for s in SNAPSHOTS.values())


@pytest.mark.parametrize("ticker", ["NVDA", "DIXON.NS"])
def test_every_numeric_field_has_a_declared_unit(ticker):
    undeclared = [p for p, _ in _numeric_fields(SNAPSHOTS[ticker])
                  if unit_of("get_company_snapshot", p) is None]
    assert undeclared == [], f"declare these in agent/field_units.py: {sorted(set(undeclared))}"


@pytest.mark.parametrize("field", ["gross_margin", "operating_margin", "net_margin",
                                   "revenue_growth", "net_income_growth"])
def test_margins_and_growth_are_fractions(field):
    assert unit_of("get_company_snapshot", f"financials.years[].{field}") == "ratio"


@pytest.mark.parametrize("ticker", ["NVDA", "DIXON.NS"])
def test_a_margin_and_a_growth_rate_support_their_percentages(ticker):
    y = _latest(ticker)
    answer = (f"Gross margin was {y['gross_margin'] * 100:.1f}% and revenue grew "
              f"{y['revenue_growth'] * 100:.1f}%.")
    assert _flagged(answer, ticker) == []


@pytest.mark.parametrize("ticker", ["NVDA", "DIXON.NS"])
def test_a_drawdown_supports_its_percentage(ticker):
    dd = SNAPSHOTS[ticker]["performance"]["max_drawdown"]
    assert dd < 0
    assert _flagged(f"The largest drop was {abs(dd) * 100:.1f}%.", ticker) == []


@pytest.mark.parametrize("ticker", ["NVDA", "DIXON.NS"])
def test_eps_is_money_and_supports_no_percentage(ticker):
    eps = _latest(ticker)["eps_diluted"]
    text = f"{eps:g}%"
    assert _flagged(f"Margins widened by {text}.", ticker) == [text]


def test_trading_days_is_a_count_and_supports_no_percentage():
    days = SNAPSHOTS["NVDA"]["performance"]["trading_days"]
    assert _flagged(f"The stock rose {days}%.", "NVDA") == [f"{days}%"]


def test_an_invented_margin_is_flagged():
    assert _flagged("Gross margin was 81.3%.", "NVDA") == ["81.3%"]
