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
"""

from __future__ import annotations

from dataclasses import dataclass

from core.numerics import candidates

__all__ = [
    "Figure", "evidence_values", "supported", "unsupported_figures",
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
DERIVED_RESCALINGS = (1.0, 100.0, 0.01)

# Above this many evidence numbers, pairwise derivation checking is skipped and
# only direct support counts. A price series carries hundreds of numbers, and
# n^2 over those buys accuracy nobody asked for at a cost inside the request
# path. Direct support still applies, so the guard gets stricter, not looser --
# which is the right direction to fail in.
DERIVATION_LIMIT = 120


@dataclass(frozen=True)
class Figure:
    """A number an answer asserts, and where in the text it was found."""

    value: float
    text: str
    start: int
    context: str

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


def evidence_values(steps) -> set[float]:
    """Numbers returned by the tool calls that succeeded this turn.

    Results only, and successes only -- see the module docstring for why
    arguments and failures are excluded.
    """
    values: set[float] = set()
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
        values.update(_walk(result))
    return values


def _close(a: float, b: float) -> bool:
    if b == 0:
        return abs(a) < 1e-12
    return abs(a - b) <= abs(b) * MATCH_RELATIVE


def _directly_supported(value: float, evidence: set[float],
                        factors: tuple[float, ...] = RESCALINGS) -> bool:
    return any(_close(value * factor, known)
               for factor in factors for known in evidence)


def _derivable(value: float, evidence: set[float]) -> bool:
    """Whether one arithmetic step over two evidence numbers reaches `value`.

    Covers the honest cases: a market cap from shares times price, a revenue
    from gross profit over a margin, a difference between two margins, a total
    from two components.
    """
    if len(evidence) > DERIVATION_LIMIT:
        return False
    values = list(evidence)
    for i, a in enumerate(values):
        for b in values[i:]:
            products = [a * b, a + b, a - b, b - a]
            if b != 0:
                products.append(a / b)
            if a != 0:
                products.append(b / a)
            for computed in products:
                if _directly_supported(value, {computed}, DERIVED_RESCALINGS):
                    return True
    return False


def supported(value: float, evidence: set[float]) -> bool:
    """Whether a figure traces to the evidence, directly or by one step."""
    if not evidence:
        return False
    return _directly_supported(value, evidence) or _derivable(value, evidence)


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
        ))
    return figures


def unsupported_figures(answer: str, steps) -> list[Figure]:
    """Figures in `answer` that no successful tool result accounts for."""
    evidence = evidence_values(steps)
    return [f for f in answer_figures(answer) if not supported(f.value, evidence)]
