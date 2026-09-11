"""Reading numbers out of written text.

Shared by two callers with opposite jobs, which is why it lives here rather
than in either of them:

- `agent/grounding.py` uses it to find the figures an answer asserts, so it can
  check each one against what the tools actually returned.
- `eval/metrics.py` uses it to find the figure an answer gives, so it can be
  scored against an expected value.

Nothing here knows about agents or evaluation. It knows that "$215.9 billion",
"215,938" and "71.07%" are numbers, that "January 31, 2026" is a date and
contains none, and that a sentence beginning "So," is more likely to hold a
conclusion than a working step.

Every rule in here was added because a real answer defeated the previous
version; the comments say which.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass

__all__ = [
    "Candidate", "candidates", "concluding_spans", "date_spans", "inside",
    "sentence_spans", "SCALES",
]


# Multipliers for a scale word, expressed in the base unit of the number.
SCALES = {
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


def date_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _DATE.finditer(text)]


# SEC form designations. "10-K" is a document name, not the number 10, and this
# corpus is made of sentences containing it.
#
# Found by the no-fabrication safety suite: the honest answer "The filing search
# failed, so I have nothing to report from the 10-K" was blocked by the
# grounding guard, which had extracted 10 as an unsupported figure. An answer
# saying it could not establish anything was being treated as a fabricated
# claim -- which makes honesty the expensive option, the exact opposite of what
# the guard is for.
#
# Matched whole, with the same span-skipping the dates use. The trailing
# "/A" covers amendments (10-K/A).
_FORM = re.compile(
    r"\b(?:10|8|6|11|15|20|40)-[A-Z]{1,3}\d?(?:/A)?\b"
    r"|\bS-\d{1,2}(?:/A)?\b"
    r"|\bSC\s+13[DG](?:/A)?\b",
)


def form_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _FORM.finditer(text)]


def inside(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def sentence_spans(text: str) -> list[tuple[int, int]]:
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
    return [(s, e) for s, e in sentence_spans(text)
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
    dates = date_spans(answer)
    forms = form_spans(answer)
    for match in _NUMBER.finditer(answer):
        # The day in "January 31, 2026" is a number in the text and never an
        # answer; so is every other component of a written date.
        if inside(match.start("digits"), dates):
            continue
        # Nor is the 10 in "10-K" a figure. See _FORM.
        if inside(match.start("digits"), forms):
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
            scale=SCALES.get(scale_word, 1.0),
            is_percent=bool(match.group("percent")),
            is_money=bool(match.group("currency")),
            grouped="," in digits,
            start=match.start(),
            text=match.group(0).strip(),
        ))
    return found
