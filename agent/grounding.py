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

Derived figures come from the calculator, and only from it
----------------------------------------------------------
Everything above about derivations describes what this guard did until
2026-11-16: it reconstructed the model's arithmetic, one step over two
operands, and accepted a figure it could reach. That made the guard a judge of
arithmetic it had to guess at, and on the Qwen eval of 2026-11-15 the model's
own arithmetic was the failure in seven of fourteen cases (26.27% for 26.23%,
65.51% for 65.47%): the guard refused them correctly and the answer was lost.

The agent now has a `calculate` tool (`tools/calculator.py`), and the rule is
simpler and stricter:

- **An answer's figure must be in a tool result.** `unsupported_figures` asks
  for direct support only. A figure the model computed itself is unsupported,
  however correct, and the retry tells it to use the calculator. A figure the
  calculator returned is in a tool result like any other.
- **A calculator operand must be in a tool result too.** Arguments are not
  evidence, and a result of `calculate` is. Without a check, an invented
  operand would go in as an argument and come out as evidence: "assume 1.8
  billion shares", multiplied, would be a grounded market value.
  `ungrounded_operands` is run by the agent loop before the tool: a numeric
  operand has to be a figure some earlier successful result of the turn
  contains, at the precision it is written to. A fact reference
  ("NVDA:revenue:2026") is looked up by the tool itself and needs no check.

The old rule is kept as `unsupported_figures_reconstructing`, unused by the
agent loop, until this has been measured on a full eval: putting it back is
then one line, and the tests that pin its behaviour still run against it.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass

from agent.field_units import unit_of
from core.numerics import candidates

__all__ = [
    "Figure", "evidence_values", "derivation_operands", "supported",
    "unsupported_figures", "unsupported_figures_reconstructing", "ungrounded_operands",
    "RESCALINGS", "MATCH_RELATIVE", "DERIVATION_LIMIT",
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

# Hyphens and dashes a model may typeset in place of "-". The answer is read as
# if it had been typed with the ASCII hyphen, so "10\u2011K" is the form name
# "10-K" and not the number 10.
#
# Found on the first hosted run (2026-11-15, openai/gpt-oss-120b): 35 of 40
# answers used U+2011 or U+202F, and a correct 71.07% draft was discarded
# because "Form 10\u2011K" was read as an unsupported 10.
#
# One character for one character, so every offset in the text is unchanged.
# Only characters that are hyphens or already count as a minus sign are mapped.
# The em dash and the horizontal bar are left alone: they are sentence
# punctuation, and "margin\u201471%" must not become minus 71.
_HYPHENS = str.maketrans({
    "\u2010": "-",   # hyphen
    "\u2011": "-",   # non-breaking hyphen
    "\u2012": "-",   # figure dash
    "\u2013": "-",   # en dash (already a sign in core.numerics)
    "\u2212": "-",   # minus sign (already a sign in core.numerics)
    "\ufe63": "-",   # small hyphen-minus
    "\uff0d": "-",   # fullwidth hyphen-minus
})


def _plain_hyphens(text: str) -> str:
    return text.translate(_HYPHENS)

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
    # Written with a percent sign or "percent": supported only by a percentage
    # or a ratio x 100 (`percent_evidence`).
    is_percent: bool = False

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


_PERCENT_KEY = re.compile(r"pct|percent", re.I)


def _walk_percent(value, tool: str | None = None, path: str = "",
                  unit: str | None = None) -> list[float]:
    """The values in a tool result that a figure written as a percentage may
    match: percentages as they are, and ratios times 100.

    - text: only numbers written with a percent sign ("29%", "71.07 percent");
    - a numeric field: by its declared unit (agent/field_units.py). A field
      named pct/percent, or whose sibling "unit" is "percent", is a percentage
      as it is; a ratio counts x 100; a level, count, amount or date is never a
      percentage (an RSI of 72.3 does not support "72.3%"). An undeclared field
      counts as a possible ratio, x 100, as before the declarations existed.
    A bare number in text ("5 of 16 rules", "Item 7") is never a percentage.
    Each value is also offered as its magnitude: "a drawdown of 31.9%" and
    "fell 12.6%" state a negative fraction without its sign.
    """
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        key = path.rsplit(".", 1)[-1].replace("[]", "")
        declared = unit_of(tool, path)
        if key in _BOOKKEEPING_KEYS and declared is None:
            return []
        if _PERCENT_KEY.search(key) or (unit or "").lower() == "percent" or declared == "percent":
            v = float(value)
        elif declared == "unit_field":
            v = float(value) if (unit or "").lower() == "percent" else float(value) * 100.0
        elif declared == "fact":
            if (unit or "").lower() != "pure":
                return []
            v = float(value) * 100.0
        elif declared in (None, "ratio"):
            v = float(value) * 100.0
        else:
            return []                       # level, count, money, date
        return [v, abs(v)] if v < 0 else [v]
    if isinstance(value, str):
        return [x for c in candidates(value) if c.is_percent
                for x in ([c.value, abs(c.value)] if c.value < 0 else [c.value])]
    if isinstance(value, dict):
        sibling_unit = value.get("unit") if isinstance(value.get("unit"), str) else None
        out: list[float] = []
        for item_key, item in value.items():
            out.extend(_walk_percent(item, tool, f"{path}.{item_key}" if path else item_key,
                                     sibling_unit))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_walk_percent(item, tool, path + "[]", unit))
        return out
    return []


def _successful_tool_results(steps):
    """(tool name, result) for each successful call, as _successful_results."""
    for step in steps:
        tool = getattr(step, "tool", None) or (
            step.get("tool") if isinstance(step, dict) else None)
        for result in _successful_results([step]):
            yield tool, result


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


def percent_evidence(steps) -> set[float]:
    """What a figure written as a percentage may match directly: the
    percentages in this turn's successful results, and their ratios x 100."""
    values: set[float] = set()
    for tool, result in _successful_tool_results(steps):
        values.update(_walk_percent(result, tool))
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


def _same_quantity(a: float, b: float) -> bool:
    """Whether `b` is `a` again, as written or as a ratio shown in percent."""
    return any(_close(a * factor, b) for factor in DERIVED_RESCALINGS)


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
        for b in values[i + 1:]:
            # One quantity is not two operands. A number against itself, or
            # against its own rendering in another scale (0.710681 and 71.07%),
            # derives 1, 100 and 0 from anything: "revenue of 100" passed on
            # 0.710681 / 0.710681 shown as a percentage.
            if _same_quantity(a, b):
                continue
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
    answer = _plain_hyphens(answer)
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
            is_percent=candidate.is_percent,
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
    """Figures in `answer` that no successful tool result contains.

    Direct support only: the figure, or a rescaling of it, is in a result. A
    figure the model derived is not accepted on the strength of arithmetic
    this guard can reconstruct; it has to have come back from the `calculate`
    tool, whose result is then where it is found. See the module docstring.

    A figure written as a percentage is matched only against percentages and
    ratios x 100 (`percent_evidence`), not against any number: "5%" is not
    supported by the 5 in "5 of 16 rules" (benchmarks/SIGNALS_EXPLAIN_7C.md,
    SBIN.NS).
    """
    evidence = evidence_values(steps)
    if not evidence:
        return answer_figures(answer)
    percents = percent_evidence(steps)
    return [figure for figure in answer_figures(answer)
            if not (_directly_supported(figure.value, percents, (1.0,)) if figure.is_percent
                    else _directly_supported(figure.value, evidence))]


def unsupported_figures_reconstructing(answer: str, steps) -> list[Figure]:
    """The rule the agent loop used until 2026-11-16, kept and not called by it.

    Accepts a figure that is in a tool result, or one arithmetic step from two
    operands. Here so the tests that pin that rule keep running, and so that
    restoring it, if a full eval says the calculator rule costs more than it
    buys, is a one-line change in the loop.
    """
    evidence = evidence_values(steps)
    operands = derivation_operands(steps)
    figures = answer_figures(answer)
    shown = {id(f): _shown_rounding(f, operands) for f in figures}
    unsupported = []
    for figure in figures:
        # The answer's own roundings of real operands, minus this figure's.
        own = {value for key, value in shown.items()
               if value is not None and key != id(figure)}
        if _stated_derivation(figure, evidence, own):
            continue
        if not supported(figure.value, evidence, operands | own, figure.half_ulp):
            unsupported.append(figure)
    return unsupported


def _written_half_ulp(raw, value: float) -> float:
    """Half a unit in the last place of an operand as the model wrote it.

    215938000000 claims every digit; "215.9 billion" claims a tenth of a
    billion; 223.71 claims a cent.
    """
    text = raw if isinstance(raw, str) else repr(raw)
    match = _DIGITS.search(text)
    if not match:
        return 0.0
    try:
        mantissa = abs(float(match.group(0).replace(",", "")))
    except ValueError:
        return 0.0
    decimals = len(match.group(1)) if match.group(1) else 0
    scale = abs(value) / mantissa if mantissa else 1.0
    return (10.0 ** -decimals) * scale / 2


_ROUNDED_OPERAND_DIGITS = 3


def _significant_digits(raw) -> int:
    """How many significant digits an operand is written to."""
    match = _DIGITS.search(raw if isinstance(raw, str) else repr(raw))
    if not match:
        return 0
    text = match.group(0).replace(",", "")
    digits = text.replace(".", "").lstrip("0")
    if "." not in text:
        digits = digits.rstrip("0")       # 24514000000 is five digits, not eleven
    return len(digits)


def ungrounded_operands(operands: dict, steps) -> list[str]:
    """The numeric operands of a `calculate` call that no tool result contains.

    `operands` maps an operand's name to (what the model sent, the number it
    parses to). Each must be a figure in an earlier successful result of this
    turn, directly or rescaled (dollars passed in billions, a ratio passed as
    a percentage), matched at the precision the operand is written to when
    that is at least three significant digits, and exactly otherwise. Figures
    only, by the rule derivations always used: a bare integer in prose
    ("Item 7") and a tool's bookkeeping (`score`, `count`) are not operands.

    Returns the names that fail, in order. An empty list means every operand
    was returned by a tool.
    """
    figures = derivation_operands(steps)
    missing = []
    for name, (raw, value) in operands.items():
        # A rounding has to say enough to be a rounding of something. "5"
        # rounds everything from 4.5 to 5.5 in nine scales; an operand written
        # to fewer than three significant digits must be a figure exactly.
        half_ulp = (_written_half_ulp(raw, value)
                    if _significant_digits(raw) >= _ROUNDED_OPERAND_DIGITS else 0.0)
        grounded = any(
            _matches_at_precision(value, known * factor, half_ulp)
            for known in figures for factor in RESCALINGS
        )
        if not grounded:
            missing.append(name)
    return missing


def _stated_derivation(figure: Figure, evidence: set[float], own: set[float]) -> bool:
    """Whether `figure` is one step from two operands the answer itself states.

    `own` holds the answer's other figures that are roundings of real operands,
    so each traces to a tool result. An answer that shows its working --
    "gross margin of $195,201 million on net sales of $416,161 million is
    46.9%" -- names the two numbers its arithmetic used, and that pair can be
    checked however much else the turn returned.

    This is tried before the wider operand set, and it is what keeps
    DERIVATION_LIMIT from refusing honest arithmetic: a turn with a whole
    income statement and one more tool result holds more than 120 operands,
    and above the limit no derivation was checked at all. The limit itself is
    unchanged and still governs derivations from operands the answer does not
    state. Nothing here accepts a figure the wider rule would refuse in a
    small turn: `own` is a subset of what that rule already searches.
    """
    if not evidence or not own:
        return False
    return _derivable(figure.value, own, figure.half_ulp)
