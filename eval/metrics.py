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

# Multipliers for a scale word, expressed in the base unit of the number.
_SCALES = {
    "thousand": 1e3, "k": 1e3,
    "million": 1e6, "m": 1e6, "mn": 1e6,
    "billion": 1e9, "bn": 1e9, "b": 1e9,
    "trillion": 1e12, "tn": 1e12, "t": 1e12,
}

# A number with whatever decoration it is wearing: currency in front, a scale
# word and/or a percent sign behind.
_NUMBER = re.compile(
    r"(?P<currency>[$€£¥])?\s*"
    r"(?P<sign>[-+−–]?)"
    r"(?P<digits>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<scale>thousand|million|billion|trillion|bn|tn|mn)?"
    r"\s*(?P<percent>%|percent(?:age point)?s?)?",
    re.I,
)

# Contexts that make a number not an answer.
_DATE_WORDS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "fiscal", "quarter", "q1", "q2", "q3", "q4", "ended", "ending", "year",
)

# A written date, matched whole so every number inside it can be excluded.
#
# The bare-year check below is not enough on its own: "January 31, 2026" made
# a diluted-EPS answer score 31.00, because 31 is not in 1900-2100 and so
# survived it. The day of the month is a number in the text and never an
# answer, so the fix is to skip anything falling inside a date's span rather
# than to test each number in isolation.
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?"
    r"|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?"
    r"|dec(?:ember)?)"
)
_DATE = re.compile(
    # January 31, 2026   Jan 25 2026   September 27th, 2025
    _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?(?:\s*,)?(?:\s*\d{4})?"
    # 31 January 2026
    r"|\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"(?:\s*,)?(?:\s*\d{4})?"
    # 2026-01-25
    r"|\d{4}-\d{2}-\d{2}"
    # 01/25/2026
    r"|\d{1,2}/\d{1,2}/\d{2,4}",
    re.I,
)

# Phrases that mark the sentence carrying the answer rather than the working.
#
# A model that shows its arithmetic mentions the inputs before the result:
# "gross profit is 153,463,000,000 ... revenue = that / 0.710681 ~
# 215,938,000,000 ... So revenue was approximately 215,938,000,000." Taking the
# first money-shaped number scored that answer as gross profit -- the agent was
# right and the harness was wrong. Taking the last unconditionally would break
# the equally common "Apple's margin was 46.91%, compared with NVIDIA's 71.07%",
# where the first is the answer. So: when a concluding sentence exists, its last
# figure wins; otherwise the first figure wins, as before.
_CONCLUSION = re.compile(
    r"\b(?:so|therefore|thus|hence|overall|in\s+summary|in\s+conclusion"
    r"|to\s+summari[sz]e|the\s+answer\s+is)\b"
    r"|\b(?:was|is|were|are|comes?\s+to|works?\s+out\s+to)\s+approximately\b"
    r"|\bestimated\s+\w+(?:\s+\w+)?\s+is\b",
    re.I,
)

# Sentence boundary: a terminator followed by space and something that starts a
# new sentence. Deliberately not a bare "\.", which would split 4.90 in two.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[$])|\n+")


def _date_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _DATE.finditer(text)]


def _inside(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_BREAK.finditer(text):
        if match.start() > start:
            spans.append((start, match.start()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def concluding_spans(text: str) -> list[tuple[int, int]]:
    """Sentences that read as stating the answer rather than working towards it."""
    return [(s, e) for s, e in _sentence_spans(text)
            if _CONCLUSION.search(text[s:e])]


@dataclass(frozen=True)
class Candidate:
    """One number found in an answer, with what it was wearing."""

    value: float          # the plain number, before any scale word
    scale: float          # the scale word's multiplier, 1.0 if none
    is_percent: bool
    is_money: bool        # carried a currency symbol
    grouped: bool         # written with thousands separators
    start: int            # where it was found, for first-wins ordering
    text: str

    @property
    def magnitude(self) -> float:
        """The value in its own base unit, scale word applied."""
        return self.value * self.scale


def _looks_like_a_year(match: re.Match, text: str) -> bool:
    """A bare 1900-2100 integer near a date word is a date, not an answer."""
    digits = match.group("digits")
    if "." in digits or "," in digits:
        return False
    try:
        value = int(digits)
    except ValueError:
        return False
    if not (1900 <= value <= 2100):
        return False
    if match.group("percent") or match.group("currency") or match.group("scale"):
        return False
    window = text[max(0, match.start() - 40):match.end() + 10].lower()
    return any(word in window for word in _DATE_WORDS)


def candidates(answer: str) -> list[Candidate]:
    """Every number in the answer that could be an answer, in order."""
    found: list[Candidate] = []
    dates = _date_spans(answer)
    for match in _NUMBER.finditer(answer):
        # The day in "January 31, 2026" is a number in the text and never an
        # answer; so is every other component of a written date.
        if _inside(match.start("digits"), dates):
            continue
        if _looks_like_a_year(match, answer):
            continue
        digits = match.group("digits")
        try:
            value = float(digits.replace(",", ""))
        except ValueError:
            continue
        if match.group("sign") in ("-", "−", "–"):
            value = -value
        scale_word = (match.group("scale") or "").lower()
        found.append(Candidate(
            value=value,
            scale=_SCALES.get(scale_word, 1.0),
            is_percent=bool(match.group("percent")),
            is_money=bool(match.group("currency")),
            grouped="," in digits,
            start=match.start(),
            text=match.group(0).strip(),
        ))
    return found


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
