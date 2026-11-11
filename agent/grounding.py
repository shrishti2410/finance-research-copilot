"""Can every figure in this answer be traced to something a tool returned?

The guard this supports used to ask a much weaker question: did *any* tool call
succeed this turn? That is not the same question, and the gap between them
produced a confidently wrong answer in the Milestone 8 eval.

Asked for NVIDIA's diluted EPS, the agent tried `calculate_ratio(diluted_eps)`
(unsupported), then `search_filings` (which crashed on a string `k`), then
`calculate_ratio(net_income)` (unsupported), then `calculate_ratio(net_margin)`
-- which succeeded. The tool it actually needed had failed; an unrelated one had
worked. The old guard saw one success and stood down. The model then wrote:

    "If we assume a typical number of shares outstanding for a company of
     NVIDIA's size ... if NVIDIA had approximately 1.8 billion shares
     outstanding, the estimated diluted EPS would be ~ 66.70"

and presented $66.70 as the answer. The real figure is $4.90; the real share
count is 24,514 million. Nothing in the turn's evidence contained 1.8 billion
or 66.70 -- the model made both up, and a successful call to a different ratio
was enough to let it through.

So the question is now per-figure, not per-turn.

What counts as support
----------------------
Only the *output* of a tool call that succeeded. Deliberately not:

- Arguments. Those are what the model said, not what it learned. Treating them
  as evidence would let a fabricated number launder itself by being passed into
  a call.
- Failed calls. An error envelope carries numbers (error codes, echoed inputs)
  and establishes nothing.

Support is not literal equality. The same value legitimately appears as
0.556025, 55.60% and "55.6 percent"; a figure in dollars gets quoted in
billions. Rescaling covers those. And a model that computes -- market cap from
a share count and a price, revenue from gross profit and a margin -- is doing
the right thing, so a value reachable by one arithmetic step from two evidence
numbers counts as supported too.

That last allowance is what keeps this from blocking correct work. In the same
eval, a *correct* answer derived revenue as gross_profit / gross_margin; both
inputs were in the tool result, and blocking it would have been the guard
punishing the model for showing its reasoning.

What a derivation may use, and how closely it must match
--------------------------------------------------------
The allowance was first written as "one arithmetic step over any two evidence
numbers, within 0.5%", and run-b of the eval (2026-11-10, case
nvda-effective-tax-rate-mdna) showed what that admits. A draft said "the net
margin was 21.93%" with no tool having returned it; the guard discarded the
draft; the retry searched the filing and restated 21.93%, and this time it went
through, because the retrieved text said "a 29% increase in compensation", the
hit's section label was "Item 7 - ...", and 29 - 7 = 22.0 is within 0.5% of
21.93. A growth rate minus an item number "derived" a margin.

Two rules close that, and `tests/test_grounding_derivation.py` pins both
alongside the derivations the allowance exists for:

- **Operands are figures.** A numeric field of a result (a price, a ratio's
  value or inputs, a statement row's absolute value), or a number in text that
  is wearing something -- a currency, a percent sign, a scale word, decimals or
  thousands grouping. A bare integer in prose or in a label ("Item 7",
  "Note 13", "page 29") is never an operand, and neither is a tool's own
  bookkeeping (`score`, `count`, `k`). Bare integers stay in the evidence for
  *direct* support, so an answer that quotes "Item 7" is not flagged.
- **The result must match to the answer's own precision.** A figure written as
  21.93% claims four significant figures; a derivation has to land within half
  a unit of its last place (0.005), not within a relative band. 22.0 is not
  21.93. Rounding is still allowed in the direction the answer rounds: "$5.5
  trillion" from 5,484,027 million is a match at half of 0.1 trillion.

Direct support keeps its relative tolerance: 0.556025 quoted as 55.60% is the
same number written to a different precision, and that is what MATCH_RELATIVE
is for.

Operands the answer itself rounded
----------------------------------
A model that computes does so on the numbers it writes down. A tool returns a
close of 223.7097, the answer says "$223.71", and the market cap it then states
-- 24,514 million x 223.71 = $5,484.03 billion -- is about $7 million away from
the product of the exact operands. Matched at the answer's precision against
the exact operands only, that correct arithmetic was refused.

So a figure the answer shows is also an operand, in the units of the operand it
rounds, when it *is* a rounding of one: within half a unit of its own last
place of a real operand (directly or as a percentage). The tolerance on the
derived figure does not change, and nothing the answer does not show is
rounded on its behalf: "$5,484.03 billion" with no rounded price in the text is
still checked against 223.7097. A figure is never an operand in its own check,
and a figure that rounds nothing in the evidence adds nothing -- which is what
keeps 21.93% from 29% and Item 7 refused.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass

from core.numerics import candidates

__all__ = [
    "Figure", "evidence_values", "derivation_operands", "supported",
    "unsupported_figures", "RESCALINGS", "MATCH_RELATIVE", "DERIVATION_LIMIT",
]

# How close a value must be to count as the same number. Relative, because the
# same figure is written to different precisions in different places.
MATCH_RELATIVE = 0.005

# Conversions that do not make a new claim: a ratio shown as a percentage, an
# amount in dollars quoted in thousands, millions or billions.
RESCALINGS = (1.0, 100.0, 0.01, 1e-3, 1e3, 1e-6, 1e6, 1e-9, 1e9)

# Derived values get a far narrower set, and the difference is load-bearing.
#
# Pairwise arithmetic over the evidence already produces a large set of
# reachable values; letting each of those be rescaled by nine factors makes the
# set dense enough to "support" almost anything. Measured on the real failing
# case: net_income x net_margin = 6.676e10, rescaled by 1e-9, lands on 66.76 --
# within tolerance of the fabricated 66.70, which the guard would then have
# waved through. A derivation that needs a scale change to match is not a
# derivation the model showed; it is a coincidence.
#
# This also means a product that is only right in different units -- "24,514"
# from a table stated in millions, times a price -- is not a derivation unless
# the evidence carries the operand in the units the answer uses. Statement rows
# carry `values_absolute` for exactly that reason.
DERIVED_RESCALINGS = (1.0, 100.0, 0.01)

# Above this many operands, pairwise derivation checking is skipped and only
# direct support counts. A price series carries hundreds of numbers, and n^2
# over those buys accuracy nobody asked for at a cost inside the request path.
# Direct support still applies, so the guard gets stricter, not looser -- which
# is the right direction to fail in.
DERIVATION_LIMIT = 120

# Numeric fields that describe the tool call rather than the world. A retrieval
# score times a growth rate is not a margin, however close it lands.
_BOOKKEEPING_KEYS = frozenset({
    "score", "vector_score", "lexical_score", "boost", "count", "k",
    "chunk_index", "rank", "iteration", "latency_ms", "days_back",
})

# Absolute slack on a precision match, so 22.28 computed as 22.279999999 is not
# refused for being a float.
_FLOAT_SLACK = 1e-9

_DIGITS = re.compile(r"\d[\d,]*(?:\.(\d+))?")

# A rendered statement says once, above the grid, what its cells are in:
# "(In millions, except per share data)". Under that note a bare "68" is a
# figure -- $68 million -- even though nothing on the cell itself says so. The
# cell counts as an operand both as written (the per-share rows are exempt from
# the note, and 223.71 / 4.90 is a real P/E) and scaled by the note (68 + 53 is
# $121 million, which is how an answer will state it).
_UNITS_NOTE = re.compile(r"\bin\s+(thousands|millions|billions)\b", re.I)
_UNITS_SCALE = {"thousands": 1e3, "millions": 1e6, "billions": 1e9}


@dataclass(frozen=True)
class Figure:
    """A number an answer asserts, and where in the text it was found."""

    value: float
    text: str
    start: int
    context: str
    # Half a unit in the last place the answer wrote, in `value`'s units. The
    # tolerance a derivation has to meet; see the module docstring.
    half_ulp: float = 0.0

    def __str__(self) -> str:
        return self.text


def _walk(value) -> list[float]:
    """Every number anywhere in a tool result, however nested."""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, str):
        return [c.value * c.scale for c in candidates(value)]
    if isinstance(value, dict):
        out: list[float] = []
        for key, item in value.items():
            out.extend(_walk(key))
            out.extend(_walk(item))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_walk(item))
        return out
    return []


def _walk_figures(value, key: str | None = None) -> list[float]:
    """The numbers in a tool result that are figures, not labels or bookkeeping.

    Numeric fields count unless their key is bookkeeping. Numbers in text count
    only when decorated -- a currency, a percent, a scale word, decimals or
    thousands grouping -- which is what separates "$18,497" and "29%" from the 7
    in "Item 7". The exception is a text carrying a units note, a rendered
    statement, where every cell is a figure and also counts scaled by the note.
    Dict keys are labels and never count.
    """
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [] if key in _BOOKKEEPING_KEYS else [float(value)]
    if isinstance(value, str):
        note = _UNITS_NOTE.search(value)
        if note is None:
            return [c.value * c.scale for c in candidates(value)
                    if c.is_percent or c.is_money or c.grouped
                    or c.scale != 1.0 or "." in c.text]
        multiplier = _UNITS_SCALE[note.group(1).lower()]
        out = []
        for c in candidates(value):
            out.append(c.value * c.scale)
            if c.scale == 1.0 and not c.is_percent:
                out.append(c.value * multiplier)
        return out
    if isinstance(value, dict):
        out: list[float] = []
        for item_key, item in value.items():
            out.extend(_walk_figures(item, item_key))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_walk_figures(item, key))
        return out
    return []


def _successful_results(steps):
    for step in steps:
        tool = getattr(step, "tool", None) or (
            step.get("tool") if isinstance(step, dict) else None)
        ok = getattr(step, "ok", None)
        if ok is None and isinstance(step, dict):
            ok = step.get("ok")
        if tool in ("final_answer", "ungrounded_answer", "tool_call_budget"):
            continue
        if not ok:
            continue
        result = getattr(step, "result", None)
        if result is None and isinstance(step, dict):
            result = step.get("result")
        yield result


def evidence_values(steps) -> set[float]:
    """Numbers returned by the tool calls that succeeded this turn.

    Results only, and successes only -- see the module docstring for why
    arguments and failures are excluded. This is the set a figure may match
    *directly*; `derivation_operands` is the narrower set arithmetic may use.
    """
    values: set[float] = set()
    for result in _successful_results(steps):
        values.update(_walk(result))
    return values


def derivation_operands(steps) -> set[float]:
    """The figures in this turn's successful results that arithmetic may combine."""
    values: set[float] = set()
    for result in _successful_results(steps):
        values.update(_walk_figures(result))
    return values


def _close(a: float, b: float) -> bool:
    if b == 0:
        return abs(a) < 1e-12
    return abs(a - b) <= abs(b) * MATCH_RELATIVE


def _directly_supported(value: float, evidence: set[float],
                        factors: tuple[float, ...] = RESCALINGS) -> bool:
    return any(_close(value * factor, known)
               for factor in factors for known in evidence)


def _matches_at_precision(value: float, computed: float, half_ulp: float) -> bool:
    """Whether `computed`, read at the answer's precision, is `value`."""
    return abs(value - computed) <= half_ulp + abs(value) * _FLOAT_SLACK


def _derivable(value: float, operands: set[float], half_ulp: float | None) -> bool:
    """Whether one arithmetic step over two operands reaches `value`.

    Covers the honest cases: a market cap from shares times price, a revenue
    from gross profit over a margin, a difference between two margins, a total
    from two components. With `half_ulp` the match is at the answer's own
    precision; without it (the legacy two-argument `supported`) it falls back to
    the relative band.
    """
    if len(operands) > DERIVATION_LIMIT:
        return False
    values = list(operands)
    for i, a in enumerate(values):
        for b in values[i:]:
            products = [a * b, a + b, a - b, b - a]
            if b != 0:
                products.append(a / b)
            if a != 0:
                products.append(b / a)
            for computed in products:
                if half_ulp is None:
                    if _directly_supported(value, {computed}, DERIVED_RESCALINGS):
                        return True
                    continue
                for factor in DERIVED_RESCALINGS:
                    if _matches_at_precision(value, computed / factor, half_ulp):
                        return True
    return False


def supported(value: float, evidence: set[float], operands: set[float] | None = None,
              half_ulp: float | None = None) -> bool:
    """Whether a figure traces to the evidence, directly or by one step.

    `operands` is what a derivation may combine (see `derivation_operands`) and
    `half_ulp` the precision it must meet. `unsupported_figures` always passes
    both. A caller that passes neither gets the older, looser derivation rule
    over the whole evidence set -- the rule that let 29 - 7 "derive" 21.93% --
    and is warned, so the orchestrator cannot fall back to it by accident.
    `tests/test_grounding_derivation.py` runs the orchestrator's path with that
    warning promoted to an error.
    """
    if not evidence:
        return False
    if _directly_supported(value, evidence):
        return True
    if operands is None:
        warnings.warn(
            "supported(value, evidence) without operands and half_ulp applies the "
            "legacy derivation rule (any two evidence numbers, 0.5% band). "
            "Production callers must go through unsupported_figures().",
            DeprecationWarning, stacklevel=2,
        )
        operands = evidence
    return _derivable(value, operands, half_ulp)


def _half_ulp(candidate) -> float:
    """Half a unit in the last place of the figure as written, in its own units."""
    match = _DIGITS.search(candidate.text)
    decimals = len(match.group(1)) if match and match.group(1) else 0
    unit = 10.0 ** -decimals
    if not candidate.is_percent:
        unit *= candidate.scale
    return unit / 2


def answer_figures(answer: str) -> list[Figure]:
    """The numbers an answer asserts.

    Dates and bare years are already excluded by `core.numerics.candidates`, so
    "fiscal year 2026" and "January 31, 2026" do not become claims that need
    supporting.
    """
    figures: list[Figure] = []
    seen: set[float] = set()
    for candidate in candidates(answer):
        value = candidate.value if candidate.is_percent else candidate.value * candidate.scale
        if any(_close(value, s) for s in seen):
            continue
        seen.add(value)
        start = max(0, candidate.start - 40)
        end = candidate.start + len(candidate.text) + 30
        figures.append(Figure(
            value=value,
            text=candidate.text,
            start=candidate.start,
            context=" ".join(answer[start:end].split()),
            half_ulp=_half_ulp(candidate),
        ))
    return figures


def _shown_rounding(figure: Figure, operands: set[float]) -> float | None:
    """The operand `figure` rounds, written as the answer wrote it, or None.

    Returned in the operand's units: "71.07%" rounding 0.710681 comes back as
    0.7107, so arithmetic with it stays in the units the evidence uses.
    """
    for factor in DERIVED_RESCALINGS:
        for known in operands:
            if _matches_at_precision(figure.value, known * factor, figure.half_ulp):
                return figure.value / factor
    return None


def unsupported_figures(answer: str, steps) -> list[Figure]:
    """Figures in `answer` that no successful tool result accounts for."""
    evidence = evidence_values(steps)
    operands = derivation_operands(steps)
    figures = answer_figures(answer)
    shown = {id(f): _shown_rounding(f, operands) for f in figures}
    unsupported = []
    for figure in figures:
        # The answer's own roundings of real operands, minus this figure's.
        own = {value for key, value in shown.items()
               if value is not None and key != id(figure)}
        if not supported(figure.value, evidence, operands | own, figure.half_ulp):
            unsupported.append(figure)
    return unsupported
