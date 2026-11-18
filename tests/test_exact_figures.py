"""The model quotes figures exactly, not rounded approximations (live-site
feedback, 2026-11-18: "around 3%" was flagged [unverified]).

The grounding guard is not loosened: a rounded figure such as "around 3%" for
a net margin of 0.028387 traces to nothing a tool returned, so it is flagged,
and should be. The fix is the instruction: state the figure as the tool
returned it (2.84%).
"""

from __future__ import annotations

import pytest

from agent.grounding import unsupported_figures
from agent.orchestrator import Step
from agent.prompts import system_prompt

pytestmark = pytest.mark.safety

PROMPT = system_prompt(5)
SNAPSHOT = {"ok": True, "ticker": "DIXON.NS",
            "financials": {"currency": "INR", "years": [{"period_end": "2025-03-31", "net_margin": 0.028387},
                                                         {"period_end": "2026-03-31", "net_margin": 0.02964}]}}


def _flagged(answer):
    step = Step(iteration=1, tool="get_company_snapshot", arguments={"ticker": "DIXON.NS"},
                result=SNAPSHOT, latency_ms=1.0, model_latency_ms=1.0, ok=True)
    return [f.text for f in unsupported_figures(answer, [step])]


def test_the_prompt_asks_for_exact_figures_and_says_why():
    text = PROMPT.lower()
    assert "exactly as the tool returned it" in text
    for word in ("around", "about", "roughly"):
        assert f'"{word}"' in text
    assert "[unverified]" in text


def test_an_exact_figure_is_supported():
    assert _flagged("Net margin was 2.84% in the year to March 2025.") == []


def test_a_rounded_approximation_is_still_flagged():
    # The guard is unchanged: this is why the prompt asks for exact figures.
    assert _flagged("Net margin was around 3% in the year to March 2025.") == ["3%"]
