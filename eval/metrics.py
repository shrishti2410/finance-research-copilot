"""Scoring functions.

The one that matters is `extract_number`: turning a written answer into a
number, deterministically.

Why not an LLM judge
--------------------
A judge is a second model that can be wrong, and its mistakes are correlated
with the system under test -- both are language models, and both are confident.
Worse, a judge makes a score unreproducible: the same run scored twice gives two
numbers, so a regression cannot be told from noise. A regex is dumber, and that
is the point: it fails the same way every time, and its failures are visible in
the report rather than smoothed into a plausible-looking percentage.

The cost is real. An answer that says "roughly seventy-one percent" scores zero
even though a person would call it correct. That is why the report separates
`wrong` from `no_number`: the first is the system being wrong, the second is
the extractor giving up, and conflating them would flatter or libel the system
depending on which way the phrasing went.

Picking the number out of prose
-------------------------------
An answer usually holds several numbers:

    "NVIDIA's gross margin for fiscal year 2026, which ended on January 31,
     2026, was 71.07%. This means that 71.07% of each sales dollar ..."

Taking the first would give 2026. So candidates are matched by *shape* against
the unit the case expects -- a percent case wants a number wearing a `%`, a
money case wants one wearing a `$` or a scale word -- and the first candidate
of the right shape wins. Years and dates are excluded outright. When nothing of
the right shape is found the extractor reports None rather than guessing from a
number of some other kind: a wrong answer scored as a pass is worse than a
correct answer scored as unparseable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "extract_number", "Candidate", "candidates", "Outcome", "score_case",
    "Summary", "summarise", "concluding_spans",
]

# The number-reading primitives live in core/numerics.py, because the grounding
# guard in agent/ needs exactly the same parsing and must not import from the
# eval harness. Re-exported here so this module's own callers are unchanged.
from core.numerics import (  # noqa: F401
    SCALES as _SCALES,
    Candidate,
    candidates,
    concluding_spans,
    date_spans as _date_spans,
    inside as _inside,
    sentence_spans as _sentence_spans,
)


# What each dataset unit is worth in the number's own terms, so a candidate can
# be converted into the unit the case is stated in.
_UNIT_BASE = {
    "USD millions": 1e6,
    "USD billions": 1e9,
    "millions of shares": 1e6,
    "billions of shares": 1e9,
}


def _convert(candidate: Candidate, unit: str) -> float | None:
    """A candidate expressed in `unit`, or None if it cannot mean that."""
    if unit in ("percent", "percentage points"):
        # Only a number actually wearing a percent sign counts. "0.71" in an
        # answer about margins is ambiguous, and guessing turns a miss into a
        # false pass.
        return candidate.value if candidate.is_percent else None

    if unit in ("USD", "ratio (x)"):
        if candidate.is_percent:
            return None
        return candidate.magnitude

    base = _UNIT_BASE.get(unit)
    if base is None:
        return None
    if candidate.is_percent:
        return None
    if candidate.scale != 1.0:
        # "$215.9 billion" against a USD-millions case -> 215900.
        return candidate.magnitude / base
    # Written out in full: "$120,067,000,000" is dollars, not millions of them.
    # Chosen by plausibility -- no line item on these statements is 10^8
    # millions ($100 trillion), so a number that big can only be the base unit.
    # This changes what a wrong answer is *reported* as, not whether it passes.
    if candidate.value >= 1e8:
        return candidate.value / base
    # A bare grouped number is already in the statement's own unit: an answer
    # that says "215,938" to a USD-millions question means millions.
    if candidate.grouped or candidate.value >= 1000:
        return candidate.value
    # A small bare number cannot be a revenue in millions; refuse rather than
    # invent a scale for it.
    return None


def extract_number(answer: str, unit: str) -> float | None:
    """The number an answer gives, in `unit`, or None if it gives none.

    Deterministic, and position-aware: if the answer has a concluding sentence
    ("So, revenue was approximately ..."), the last figure in the last such
    sentence wins, because that is the stated result rather than an input to
    it. Otherwise the first figure wins, which is right for the ordinary
    "X was 46.91%, compared with Y's 71.07%" shape.
    """
    if not answer:
        return None

    usable = [(c, _convert(c, unit)) for c in candidates(answer)]
    usable = [(c, v) for c, v in usable if v is not None]
    if not usable:
        return None

    concluding = concluding_spans(answer)
    if concluding:
        # The last concluding sentence, and within it the last figure: a
        # conclusion that restates its inputs puts the result last.
        last_start, last_end = concluding[-1]
        in_last = [v for c, v in usable if last_start <= c.start < last_end]
        if in_last:
            return in_last[-1]

    return usable[0][1]


# ── scoring ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Outcome:
    """What happened to one case."""

    case_id: str
    question: str
    expected: float
    unit: str
    tolerance: float
    answerable_via: tuple[str, ...]
    company: str
    extracted: float | None
    answer: str
    passed: bool
    latency_ms: float
    tool_calls: int
    iterations: int
    prompt_tokens: int
    completion_tokens: int
    completed: bool
    stop_reason: str
    error: str = ""

    @property
    def verdict(self) -> str:
        if self.error:
            return "error"
        if self.extracted is None:
            return "no_number"
        return "pass" if self.passed else "wrong"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def score_case(case, answer: str, **fields) -> Outcome:
    """Apply the case's tolerance to whatever number the answer gave."""
    extracted = extract_number(answer, case.unit)
    return Outcome(
        case_id=case.id,
        question=case.question,
        expected=case.expected_answer,
        unit=case.unit,
        tolerance=case.tolerance,
        answerable_via=case.answerable_via,
        company=case.company,
        extracted=extracted,
        answer=answer,
        passed=case.matches(extracted),
        **fields,
    )


@dataclass
class Summary:
    """Aggregates over a set of outcomes."""

    label: str
    total: int
    passed: int
    wrong: int
    no_number: int
    errors: int
    mean_latency_ms: float
    mean_tool_calls: float
    mean_iterations: float
    prompt_tokens: int
    completion_tokens: int

    @property
    def accuracy(self) -> float:
        return (self.passed / self.total * 100) if self.total else 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def summarise(label: str, outcomes: list[Outcome]) -> Summary:
    n = len(outcomes)
    mean = lambda xs: (sum(xs) / n) if n else 0.0  # noqa: E731
    return Summary(
        label=label,
        total=n,
        passed=sum(1 for o in outcomes if o.verdict == "pass"),
        wrong=sum(1 for o in outcomes if o.verdict == "wrong"),
        no_number=sum(1 for o in outcomes if o.verdict == "no_number"),
        errors=sum(1 for o in outcomes if o.verdict == "error"),
        mean_latency_ms=mean([o.latency_ms for o in outcomes]),
        mean_tool_calls=mean([o.tool_calls for o in outcomes]),
        mean_iterations=mean([o.iterations for o in outcomes]),
        prompt_tokens=sum(o.prompt_tokens for o in outcomes),
        completion_tokens=sum(o.completion_tokens for o in outcomes),
    )
