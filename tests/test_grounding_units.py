"""A percentage is supported only by a percentage, or by a ratio times 100.

benchmarks/SIGNALS_EXPLAIN_7C.md, SBIN.NS: an explanation said the signal had
"beaten only 5% of 200 random re-timings" where the tool said 0%, and the
grounding guard let it through. The guard matched "5%" against any 5 in the
evidence, such as the 5 in "5 of 16 rules" or a 5 bps slippage. A count is not
a percentage.

A figure written with a percent sign (or "percent") now needs one of:
- a percentage in a tool result: text with a percent sign, a numeric field
  whose key says pct/percent/percentage, or a numeric field whose sibling
  "unit" is "percent" (the calculator's percent operations);
- a numeric field read as a ratio and multiplied by 100 (a margin of 0.7107
  stated as 71.07%).
Figures without a percent sign keep the rules they had.
"""

from __future__ import annotations

import pytest

from agent.grounding import unsupported_figures
from agent.orchestrator import Step

pytestmark = pytest.mark.safety


def _ok(tool: str, result) -> Step:
    return Step(iteration=1, tool=tool, arguments={}, result=result,
                latency_ms=1.0, model_latency_ms=1.0, ok=True)


SIGNAL = [_ok("get_trading_signal", {
    "ticker": "SBIN.NS", "coverage": "5 of 16 rules", "slippage_bps": 5,
    "random": {"runs": 200, "signal_sharpe_percentile": 0.0},
    "verdict": "beat 0% of 200 random re-timings",
})]


def _flagged(answer, steps):
    return [f.text for f in unsupported_figures(answer, steps)]


def test_the_sbin_case_a_count_does_not_support_a_percentage():
    assert _flagged("It beat only 5% of 200 random re-timings.", SIGNAL) == ["5%"]


def test_the_true_figure_from_the_same_result_is_supported():
    assert _flagged("It beat 0% of 200 random re-timings, on 5 of 16 rules.", SIGNAL) == []


def test_a_ratio_field_supports_its_percentage():
    steps = [_ok("calculate_ratio", {"ratio": "gross_margin", "value": 0.710681})]
    assert _flagged("Gross margin was 71.07%.", steps) == []


def test_percent_text_supports_the_same_percentage():
    steps = [_ok("search_filings", {"hits": [{"content": "Data Center revenue grew 29% year over year."}]})]
    assert _flagged("Data Center revenue grew 29%.", steps) == []


def test_a_pct_field_supports_its_value_as_a_percentage():
    steps = [_ok("get_quote", {"ticker": "SBIN.NS", "change_pct": 1.66})]
    assert _flagged("SBIN rose 1.66% on the day.", steps) == []


def test_the_calculators_percent_result_supports_it():
    steps = [_ok("calculate", {"ok": True, "value": 21.9312, "unit": "percent",
                               "formatted": "21.93%", "formula": "x / y * 100 = 21.9312%"})]
    assert _flagged("Net margin was 21.93%.", steps) == []


def test_a_plain_numeric_field_is_not_a_percentage_as_written():
    # 12 of something is not 12%; but 0.12 as a ratio would be.
    steps = [_ok("get_trading_signal", {"rules_counted": 12, "confidence": 0.12})]
    assert _flagged("The vote carried 12% of the weight.", steps) == []      # 0.12 x 100
    steps = [_ok("get_trading_signal", {"rules_counted": 12})]
    assert _flagged("The vote carried 12% of the weight.", steps) == ["12%"]


def test_numbers_in_plain_text_do_not_support_a_percentage():
    steps = [_ok("search_filings", {"hits": [{"content": "Item 7. We have 29 offices in 12 countries."}]})]
    assert _flagged("Revenue grew 29%.", steps) == ["29%"]


def test_figures_without_a_percent_sign_keep_their_rules():
    steps = [_ok("get_trading_signal", {"coverage": "5 of 16 rules", "revenue": 416161000000})]
    assert _flagged("It counted 5 of 16 rules; revenue was $416.161 billion.", steps) == []
