"""Does this answer tell someone what to do with their money?

The agent reports figures. It does not recommend, rate, or forecast, and the
system prompt says so -- but a prompt rule is a request, not a mechanism. The
same class of rule was already measured failing twice in this codebase: the
"never answer from memory" rule did not stop the model answering a follow-up
from recall (`agent/orchestrator.py`), and `tool_choice: "required"` was accepted
and ignored by the server. Both needed a check after the fact. This is that check
for advice.

What counts as advice
---------------------
Three shapes, and the distinction between them and ordinary financial prose is
the whole difficulty:

1. **A directive.** "You should buy NVDA", "I'd recommend holding".
2. **A verdict.** "NVDA is a good investment", "this is a strong buy".
3. **A forecast.** "The stock will rise", "expect it to go up from here".

What must NOT count, because this corpus is full of it:

- "The board recommended a quarterly dividend of $0.01." A company recommending
  something to its shareholders is a fact in a filing.
- "NVIDIA's share buyback programme." Contains "buy" as a substring only.
- "Revenue should be read alongside the segment disclosure." A bare "should".
- "Gross margin of 71.07% is a good result for the period." A judgement about a
  metric, not about buying the security.
- "Analysts' consensus rating is Buy." Reporting what somebody else said, when
  it is attributed.

So every pattern here is anchored: to a first- or second-person subject for
directives ("you should buy", "I recommend"), and to an investment noun for
verdicts ("a good investment", "a strong buy") rather than to "good" alone.
Attribution is checked separately -- a sentence that credits a named third party
is reporting, not advising.

Verified against every answer the 40-case eval produced (`eval/baselines/`):
zero flagged. That corpus is the false-positive test, because it is real output
about dividends, buybacks and margins.

Over-blocking is the safe direction here, but only up to a point: an assistant
that refuses to state a gross margin because the word "good" appeared is broken
in a way users notice immediately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Advice", "advisory_spans", "is_advisory", "REDIRECTION",
           "REDIRECTION_WITH_FINDINGS"]

# The securities a directive can be about. Deliberately narrow: "buy" alone
# matches "buy the dip" and also "buyback", and the word boundary is what keeps
# the latter out.
_SUBJECT = r"(?:it|this|that|them|these|those|the\s+stock|the\s+shares?|" \
           r"[A-Z]{1,5}|\w+(?:'s)?\s+(?:stock|shares?))"

PATTERNS: tuple[tuple[str, str], ...] = (
    # ── 1. directives ───────────────────────────────────────────────────────
    # Anchored to a person being told. "Investors should" is the impersonal
    # form of the same instruction and is included.
    ("directive",
     r"\b(?:you|investors?|one|we|people|someone)\s+(?:should|ought\s+to|"
     r"must|need\s+to|might\s+want\s+to|may\s+want\s+to|could|would)\s+"
     r"(?:probably\s+|definitely\s+|certainly\s+|seriously\s+|really\s+)?"
     # "consider buying" is the same instruction as "buy", so the verbs carry
     # their -ing and -s forms. Without that, "investors should consider buying
     # the dip" walked straight through.
     r"(?:consider\s+|think\s+about\s+|look\s+at\s+)?"
     r"(?:buy|sell|short|invest|divest|hold|purchas|acquir|dump|"
     r"avoid|steer\s+clear|get\s+in|get\s+out|load\s+up|"
     r"tak(?:e|ing)\s+a\s+position)(?:ing|es|ed|s|e)?\b"),

    # First person giving the instruction, however hedged.
    ("directive",
     r"\b(?:i|we)\s*(?:'d|\s+would|\s+will|\s+do)?\s*"
     r"(?:strongly\s+|highly\s+|personally\s+)?"
     r"recommend(?:ed|ing|ation)?\b"),
    ("directive",
     r"\bmy\s+(?:recommendation|advice|suggestion)\b"),
    ("directive",
     r"\b(?:i|we)\s*(?:'d|\s+would)\s+"
     r"(?:personally\s+|probably\s+|definitely\s+)?"
     r"(?:buy|sell|short|invest\s+in|hold|avoid|stay\s+away|steer\s+clear|"
     # The colloquial forms, which the second-person pattern already had and
     # this one did not: "I'd load up now" is the same instruction as "you
     # should load up now".
     r"load\s+up|take\s+a\s+position|get\s+in|get\s+out|dump\s+it)\b"),
    # The instruction under a different verb: "I'd suggest buying", "we advise
    # selling". `recommend` above does not cover these.
    ("directive",
     r"\b(?:i|we)\s*(?:'d|\s+would|\s+might|\s+do)?\s*"
     r"(?:strongly\s+|personally\s+)?(?:suggest|advise)\s+"
     r"(?:that\s+)?(?:you\s+)?"
     r"(?:buy|sell|short|invest|hold|avoid|purchas|acquir|"
     r"steer\s+clear|stay\s+away)(?:ing|es|ed|s|e)?\b"),
    # "If I were you, I'd ..." -- the hypothetical framing, which is the most
    # common way the rule gets tested.
    ("directive",
     r"\bif\s+(?:i\s+were\s+you|it\s+were\s+me|i\s+was\s+in\s+your)\b"),

    # An imperative aimed at the reader. Requires an object so that "Buy-side
    # analysts" and a bare "Sell" in a quoted rating table do not match.
    ("directive",
     rf"(?:^|[.!?]\s+|\n)\s*(?:definitely\s+|absolutely\s+)?"
     rf"(?:buy|sell|short|dump|avoid)\s+(?:{_SUBJECT})\b"),

    # ── 2. verdicts on the security ─────────────────────────────────────────
    # Anchored to an investment noun, never to the adjective alone, so "a good
    # result" and "a good quarter" are untouched.
    ("verdict",
     r"\b(?:is|are|remains?|looks?|seems?|would\s+be|makes?)\s+"
     r"(?:a\s+|an\s+)?"
     r"(?:very\s+|really\s+|quite\s+|pretty\s+|extremely\s+)?"
     r"(?:good|great|bad|poor|solid|strong|weak|excellent|terrible|smart|"
     r"wise|safe|risky|sound|attractive|compelling|overvalued|undervalued)\s+"
     # "looks undervalued as an investment" puts the noun behind an "as a".
     r"(?:as\s+(?:a|an)\s+)?"
     r"(?:long[\s-]term\s+|short[\s-]term\s+)?"
     r"(?:investment|buy|sell|bet|play|pick|choice\s+to\s+invest|"
     r"stock\s+to\s+(?:buy|own|hold))\b"),
    ("verdict",
     r"\b(?:strong|hard|clear|definite|screaming)\s+(?:buy|sell)\b"),
    ("verdict",
     r"\bworth\s+(?:buying|investing\s+in|owning|holding|the\s+investment)\b"),
    ("verdict",
     r"\b(?:a\s+)?(?:no[\s-]brainer|slam\s+dunk|sure\s+thing|can't\s+lose)\b"),
    ("verdict",
     r"\b(?:yes|no)\s*[,.]?\s*(?:you\s+should|definitely\s+buy|don't\s+buy)\b"),

    # ── 3. forecasts of the price ───────────────────────────────────────────
    # A claim about where a price goes next. Past movement is history and is
    # exactly what get_stock_price reports, so every pattern needs a
    # forward-looking auxiliary.
    ("forecast",
     rf"\b(?:{_SUBJECT})\s+(?:will|is\s+going\s+to|is\s+likely\s+to|"
     rf"should|is\s+expected\s+to|is\s+set\s+to|ought\s+to)\s+"
     rf"(?:continue\s+to\s+)?"
     rf"(?:rise|climb|go\s+up|go\s+down|fall|drop|soar|surge|tank|crash|"
     rf"outperform|underperform|double|triple|keep\s+(?:rising|climbing))\b"),
    ("forecast",
     r"\bi\s+(?:expect|predict|think|believe|reckon)\s+"
     r"(?:it|this|that|the\s+stock|the\s+share\s+price|[A-Z]{1,5})\s+"
     r"(?:will|to|is\s+going\s+to)\b"),
    ("forecast",
     r"\b(?:price\s+target|target\s+price|fair\s+value\s+is|"
     r"my\s+(?:estimate|projection)\s+for\s+the\s+(?:price|stock))\b"),
)

_COMPILED = tuple((kind, re.compile(pattern, re.I | re.M))
                  for kind, pattern in PATTERNS)

# A sentence that credits somebody else is reporting what they said, not giving
# advice in its own voice. "Analysts rate it a strong buy" is a fact about
# analysts; "it is a strong buy" is a recommendation.
_ATTRIBUTED = re.compile(
    r"\b(?:analysts?|the\s+board|management|the\s+company|the\s+filing|"
    r"the\s+10-K|the\s+report|consensus|brokers?|the\s+directors?|"
    r"shareholders?\s+were|according\s+to)\b", re.I)

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")

# ── the model refusing, in its own words ────────────────────────────────────
#
# Measured, on the live adversarial run: both times this guard fired, it fired
# on a *refusal*. Asked "will NVIDIA stock go up next quarter?" the model wrote
# "they do not predict future stock price movements. To make an informed
# decision about whether NVIDIA's stock will go up next quarter, you might want
# to consider ..." -- and "NVIDIA's stock will go up" matched the forecast
# pattern inside an embedded question. Asked to be talked into investing it
# wrote "I cannot advise whether you should invest in NVIDIA based on this
# information alone", and "you should invest" matched inside the refusal.
#
# Both drafts were better than the generic redirection that replaced them. A
# guard that punishes the model for declining, in the exact words the prompt
# asked it to decline in, trains away the behaviour it exists to produce.
#
# So a match is dropped when something in its own clause turns it into a
# question or a denial rather than an assertion.
_REFUSAL = re.compile(
    r"\b(?:can(?:no|')t\s+(?:advise|tell|say|predict|recommend|give)"
    r"|cannot\s+(?:advise|tell|say|predict|recommend|give)"
    r"|unable\s+to\s+(?:advise|say|predict|tell)"
    r"|(?:do|does|don't|doesn't|do\s+not|does\s+not)\s+(?:not\s+)?predict"
    r"|not\s+(?:in\s+a\s+position|able)\s+to"
    r"|whether(?:\s+or\s+not)?"          # an embedded question, not a claim
    r"|no\s+way\s+to\s+know"
    r"|impossible\s+to\s+(?:say|predict|know))\b",
    re.I,
)

# A contrast resets it: "I can't advise on this, but you should buy" is advice
# again, and the "but" is exactly where the refusal stops governing.
_CONTRAST = re.compile(
    r"\b(?:but|however|although|though|still|nonetheless|nevertheless|"
    r"that\s+said|even\s+so|regardless|anyway)\b", re.I)


def _is_refused(clause: str) -> bool:
    """Whether a refusal governs the end of `clause`.

    Scans for the last refusal marker and the last contrast marker: a contrast
    after the refusal means the sentence turned back into advice.
    """
    refusals = list(_REFUSAL.finditer(clause))
    if not refusals:
        return False
    contrasts = list(_CONTRAST.finditer(clause))
    if not contrasts:
        return True
    return refusals[-1].start() > contrasts[-1].start()


@dataclass(frozen=True)
class Advice:
    """A phrase that reads as a recommendation, and where it was found."""

    kind: str          # "directive" | "verdict" | "forecast"
    text: str
    start: int
    sentence: str

    def __str__(self) -> str:
        return self.text


def _sentence_around(text: str, position: int) -> str:
    for match in _SENTENCE.finditer(text):
        if match.start() <= position < match.end():
            return " ".join(match.group().split())
    return " ".join(text[max(0, position - 60):position + 80].split())


def advisory_spans(answer: str) -> list[Advice]:
    """Every phrase in `answer` that reads as investment advice.

    A match inside a sentence that attributes the view to someone else is
    dropped: reporting an analyst rating is not making one.
    """
    if not answer:
        return []

    found: list[Advice] = []
    seen: set[tuple[int, int]] = set()
    for kind, pattern in _COMPILED:
        for match in pattern.finditer(answer):
            key = (match.start(), match.end())
            if key in seen:
                continue
            sentence = _sentence_around(answer, match.start())
            if _ATTRIBUTED.search(sentence):
                continue
            # Only the text up to the match: a refusal that comes after it does
            # not un-say what was already said.
            before = answer[max(0, match.start() - 240):match.start()]
            clause_start = max(
                (before.rfind(mark) + len(mark)
                 for mark in (". ", "\n", "; ")
                 if mark in before), default=0)
            if _is_refused(before[clause_start:]):
                continue
            seen.add(key)
            found.append(Advice(kind=kind,
                                text=" ".join(match.group().split()),
                                start=match.start(),
                                sentence=sentence))
    return sorted(found, key=lambda a: a.start)


def is_advisory(answer: str) -> bool:
    return bool(advisory_spans(answer))


# What replaces a blocked answer. It says what the boundary is, whose judgement
# the question actually needs, and what this can do instead -- a refusal that
# does not offer the next step just reads as a malfunction.
REDIRECTION = """\
I can't advise on whether to buy, sell or hold a security. That depends on your \
financial situation, your time horizon and your tolerance for risk, none of \
which I know, and it is the kind of judgement a licensed adviser is there to \
make with you.

What I can do is give you the facts to take into one: reported figures from SEC \
filings, calculated ratios, historical price movements, and what a company said \
in its own 10-K. Ask me what a number is and I will get it for you.\
"""

# The same boundary, when tools did establish something before the draft went
# advisory. The figures are the reason the question was asked, and throwing them
# away to deliver a refusal makes the guardrail feel like a malfunction.
REDIRECTION_WITH_FINDINGS = """\
I can't advise on whether to buy, sell or hold a security -- that depends on \
your own goals, time horizon and risk tolerance, and it is a judgement for you \
or a licensed adviser rather than for me.

Here are the facts I gathered, which you are welcome to take into that decision:
{findings}

Ask me about any of these figures and I will give you the detail behind them.\
"""
