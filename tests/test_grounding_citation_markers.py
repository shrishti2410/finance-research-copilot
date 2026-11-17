"""Citation markers are not figures.

gpt-oss writes citation markers such as 【0†value】 or 【3†L10-L22】 into its
answers. The grounding guard read the digits inside them as figures no tool
returned and put the [Unverified] banner on four correct answers in the
2026-11-17 calculator run (benchmarks/HOSTED_RUN_2026-11-17_CALCULATOR.md:
nvda-diluted-shares-fy2026, aapl-revenue-fy2025, aapl-services-fy2025,
aapl-net-income-fy2023).

The markers are blanked before figures are read. Only a marker of that exact
shape is blanked: the figures beside it are still checked, and an untraced
figure next to a marker is still flagged.
"""

from __future__ import annotations

import pytest

from agent.grounding import answer_figures, unsupported_figures
from agent.orchestrator import Step

pytestmark = pytest.mark.safety


def _ok(tool: str, result) -> Step:
    return Step(iteration=1, tool=tool, arguments={}, result=result,
                latency_ms=1.0, model_latency_ms=1.0, ok=True)


REVENUE = [_ok("get_financial_facts", {"ticker": "AAPL", "facts": [
    {"concept": "Revenues", "value": 416161000000, "fiscal_year": 2025}]})]


@pytest.mark.parametrize("marker", ["【0†value】", "【3†L10-L22】", "【12†source】", "【0†Revenues】"])
def test_a_marker_after_a_supported_figure_raises_no_flag(marker):
    answer = f"Apple's total net sales in fiscal 2025 were $416.161 billion{marker}."
    assert unsupported_figures(answer, REVENUE) == []


def test_the_four_answers_from_the_calculator_run_shape():
    # The shape of the four false banners: the figure, then a marker.
    answer = "NVIDIA reported diluted shares of 24.8 billion【0†value】 for fiscal 2026."
    steps = [_ok("get_financial_facts", {"facts": [{"value": 24804000000}]})]
    assert unsupported_figures(answer, steps) == []


def test_an_untraced_figure_beside_a_marker_is_still_flagged():
    answer = "Revenue was $416.161 billion【0†value】 and services margin was 99.9%【1†value】."
    flagged = [f.text for f in unsupported_figures(answer, REVENUE)]
    assert flagged == ["99.9%"]


def test_figures_keep_their_positions_in_the_original_answer():
    answer = "Revenue【0†value】 was $416.161 billion."
    (figure,) = answer_figures(answer)
    assert answer[figure.start:].startswith("$416.161")


@pytest.mark.parametrize("text, kept", [
    ("Revenue was [0] $416.161 billion.", 0.0),        # no dagger: not this kind of marker
    ("Revenue rose 7【no closing bracket.", 7.0),     # unclosed: not a marker
])
def test_only_a_closed_dagger_marker_is_blanked(text, kept):
    assert kept in [f.value for f in answer_figures(text)]
