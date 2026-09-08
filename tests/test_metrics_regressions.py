"""Regressions from Run A of the end-to-end eval.

Both answers below are verbatim from stored `messages.meta` rows -- not
paraphrases. Each one was scored wrong by the extractor for a reason that had
nothing to do with the agent, which is the failure mode these pin: a harness
bug reads as a system bug, and the system gets blamed for the harness.
"""

import pytest

from eval.metrics import candidates, concluding_spans, extract_number

# Verbatim answer, message id 1445. The agent answered CORRECTLY -- 215,938
# million is the expected value -- and the extractor scored it 153,463 because
# that is the first money-shaped number and the model showed its working.
RUN_A_REVENUE_ANSWER = (
    "To answer your question, I need to calculate the total revenue for "
    "NVIDIA's fiscal year 2026. However, the information provided only gives "
    "us the gross margin for that fiscal year, which is 71.07%. To get the "
    "total revenue, we would need the gross profit figure, which is provided "
    "as 153,463,000,000 USD. Using the gross margin formula, we can calculate "
    "the total revenue. The gross margin formula is:\n\n"
    "\\[ \\text{Gross Margin} = \\frac{\\text{Gross Profit}}{\\text{Revenue}} \\]\n\n"
    "Rearranging for Revenue, we get:\n\n"
    "\\[ \\text{Revenue} = \\frac{\\text{Gross Profit}}{\\text{Gross Margin}} \\]\n\n"
    "Plugging in the numbers:\n\n"
    "\\[ \\text{Revenue} = \\frac{153,463,000,000 \\text{ USD}}{0.710681} "
    "\\approx 215,938,000,000 \\text{ USD} \\]\n\n"
    "So, NVIDIA's total revenue for fiscal year 2026 was approximately "
    "215,938,000,000 USD."
)

# Verbatim answer, message id 1419. The agent was genuinely wrong here -- it
# invented a share count and produced $66.70 against an expected 4.90 -- but
# the extractor reported 31.00, the day in "January 31, 2026".
RUN_A_EPS_ANSWER = (
    "The net margin for NVIDIA for the most recent fiscal year (ending "
    "January 31, 2026) is 55.60%. This means that for every dollar of "
    "revenue, NVIDIA retains approximately 55.60 cents as net income.\n\n"
    "To estimate the diluted earnings per share (diluted EPS), we would "
    "typically divide the net income by the weighted average number of shares "
    "outstanding. However, since we do not have the exact number of shares "
    "outstanding, we can use the net income and the net margin to provide a "
    "rough estimate.\n\n"
    "Given the net income of $120,067,000,000 and a net margin of 55.60%, we "
    "can estimate the diluted EPS as follows:\n\n"
    "\\[ \\text{Diluted EPS} = \\frac{\\text{Net Income}}"
    "{\\text{Weighted Average Number of Shares Outstanding}} \\]\n\n"
    "Without the exact number of shares outstanding, we can use the net "
    "income and net margin to provide a rough estimate. If we assume a "
    "typical number of shares outstanding for a company of NVIDIA's size, we "
    "can make an educated guess. For example, if NVIDIA had approximately 1.8 "
    "billion shares outstanding, the estimated diluted EPS would be:\n\n"
    "\\[ \\text{Diluted EPS} = \\frac{120,067,000,000}{1,800,000,000} "
    "\\approx 66.70 \\]\n\n"
    "Therefore, the estimated diluted earnings per share for NVIDIA for the "
    "most recent fiscal year is approximately $66.70 per share. Please note "
    "that this is an estimate and the actual figure may differ based on the "
    "exact number of shares outstanding."
)


# ── bug 1: the working, not the conclusion ──────────────────────────────────

def test_shown_arithmetic_extracts_the_result_not_the_input():
    """Run A, nvda-revenue-fy2026. Expected 215,938; the extractor returned
    153,463 -- the gross profit the model divided by on its way there."""
    assert extract_number(RUN_A_REVENUE_ANSWER, "USD millions") == 215938.0


def test_the_old_first_match_value_is_no_longer_returned():
    """Named explicitly so a future change that reintroduces first-wins fails
    here rather than silently re-scoring a correct answer as wrong."""
    assert extract_number(RUN_A_REVENUE_ANSWER, "USD millions") != 153463.0


def test_that_answer_now_passes_its_case():
    from eval.datasets import load_cases

    case = next(c for c in load_cases() if c.id == "nvda-revenue-fy2026")
    assert case.matches(extract_number(RUN_A_REVENUE_ANSWER, case.unit))


def test_the_concluding_sentence_is_the_one_that_is_found():
    spans = concluding_spans(RUN_A_REVENUE_ANSWER)
    assert spans
    last = RUN_A_REVENUE_ANSWER[spans[-1][0]:spans[-1][1]]
    assert last.startswith("So, NVIDIA's total revenue")


def test_a_comparison_without_a_conclusion_still_takes_the_first_figure():
    """Taking the last number unconditionally would break this shape, which is
    at least as common as shown working."""
    answer = "Apple's gross margin was 46.91%, compared with NVIDIA's 71.07%."
    assert extract_number(answer, "percent") == 46.91


def test_a_restating_conclusion_still_yields_the_result():
    answer = ("Gross profit was $153,463 million and revenue $215,938 million. "
              "So the revenue figure is approximately $215,938 million.")
    assert extract_number(answer, "USD millions") == 215938.0


# ── bug 2: the day of the month ─────────────────────────────────────────────

def test_a_day_of_month_is_not_a_dollar_figure():
    """Run A, nvda-diluted-eps-fy2026. The extractor returned 31.00 -- the day
    in 'January 31, 2026'."""
    assert extract_number(RUN_A_EPS_ANSWER, "USD") != 31.0


def test_that_answer_now_extracts_the_figure_the_agent_actually_gave():
    """66.70 is wrong -- the agent invented a share count -- but it is what the
    answer says, and the eval must score the agent's claim, not a date."""
    assert extract_number(RUN_A_EPS_ANSWER, "USD") == 66.70


def test_the_case_still_fails_because_the_agent_was_wrong():
    """Fixing extraction must not turn a genuine miss into a pass."""
    from eval.datasets import load_cases

    case = next(c for c in load_cases() if c.id == "nvda-diluted-eps-fy2026")
    assert not case.matches(extract_number(RUN_A_EPS_ANSWER, case.unit))


@pytest.mark.parametrize("text,day", [
    ("The period ended January 31, 2026 was strong.", 31.0),
    ("Filed on 25 January 2026 with the SEC.", 25.0),
    ("The quarter ended Sept 27, 2025 as reported.", 27.0),
    ("Reported for 2026-01-25 in the filing.", 25.0),
])
def test_no_component_of_a_written_date_is_a_candidate(text, day):
    assert all(abs(c.value - day) > 1e-9 for c in candidates(text))


def test_a_price_next_to_a_date_survives():
    """Only the date's own span is excluded, not the numbers around it."""
    answer = "On August 7, 2026 the stock closed at $223.96."
    assert extract_number(answer, "USD") == 223.96


def test_a_bare_month_is_not_treated_as_a_date_span():
    answer = "The stock fell -5.53% in June."
    assert extract_number(answer, "percent") == -5.53
