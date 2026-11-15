"""A year that labels a filing or a fiscal period is not a figure.

core/numerics.py is read by the grounding guard and by the scorer, so each rule
is pinned from both sides: what stops being a candidate, and what must not.
"""

from __future__ import annotations

import pytest

from core.numerics import candidates
from eval.metrics import extract_scored


def _values(text: str) -> list[float]:
    return [c.value for c in candidates(text)]


@pytest.mark.parametrize("text", [
    "as shown in the MD&A section of its 2026 Form 10-K",
    "per the 2026 10-K",
    "in its 2025 annual report on Form 10-K",
    "the 2026 Annual Report on Form 10-K/A",
    "from the Form 10-K for 2026",
    "the 10-K (2025) says so",
    "the 10-K filing for fiscal 2026",
    "the 10-K for the year 2026",
    "the 20-F for 2024",
    "Apple's FY2026 results",
    "Apple's FY 2026 results",
    "NVIDIA's FY-2026 figures",
])
def test_a_year_that_labels_a_filing_or_fiscal_year_is_not_a_candidate(text):
    assert _values(text) == []


@pytest.mark.parametrize("text, expected", [
    # The label is excused; the figure beside it is not.
    ("Its 2026 Form 10-K reports revenue of 2,026 million.", [2026.0]),
    ("The 2026 Form 10-K shows a margin of 71.07%.", [71.07]),
    ("FY2026 revenue was $215.9 billion.", [215.9]),
    ("The 10-K for 2026 lists 2,050 employees.", [2050.0]),
    # A decorated number in the year range is a figure wherever it sits.
    ("The 10-K reports $2026 in fees.", [2026.0]),
    ("The 10-K shows 2026% growth.", [2026.0]),
    ("The 10-K reports 2026.5 units.", [2026.5]),
    # A year-like number that is not against the form name is still a candidate.
    ("The 10-K says the company shipped 2026 units.", [2026.0]),
    ("Headcount was 2026 according to the company's Form 10-K.", [2026.0]),
    ("There were 2026 stores.", [2026.0]),
    # Outside the year range the form name excuses nothing.
    ("The 10-K for 3000 units", [3000.0]),
    ("FY 26 guidance", [26.0]),
])
def test_what_the_rule_does_not_excuse(text, expected):
    assert _values(text) == expected


def test_the_form_number_itself_is_still_skipped():
    assert _values("its 2026 Form 10-K and its 2025 Form 8-K") == []


def test_the_scorer_reads_the_same_answer_as_before():
    """The grader side: the figure scored does not move."""
    answer = ("NVIDIA reported a gross margin of 71.07% for its fiscal year 2026, as "
              "shown in the MD&A section of its 2026 Form 10-K.")
    assert extract_scored(answer, None, "percent")[0] == pytest.approx(71.07)
    assert extract_scored(answer, "71.07%", "percent")[0] == pytest.approx(71.07)


def test_an_answer_that_is_only_a_filing_label_scores_as_no_number():
    """Before the rule this scored 2026 as the answer, which is wrong either way."""
    assert extract_scored("See the 2026 Form 10-K.", None, "percent")[0] is None
