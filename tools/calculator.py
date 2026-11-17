"""Arithmetic done by code, on operands that name where they came from.

    await calculate("percent_change", "NVDA:revenue:2025", "NVDA:revenue:2026")
    await calculate("multiply", 24514000000, 223.7097,
                    a_label="NVDA FY2026 diluted shares", b_label="NVDA close 2026-08-07")

Why this exists
---------------
On the Qwen eval of 2026-11-15 (merged step 3, commit 6b8b9ae) seven of the
fourteen failures had the right inputs in hand and the wrong arithmetic:
109,158 / 416,161 written as 26.27% (it is 26.23%), revenue growth as 65.51%
(65.47%), a share count multiplied through as 2.45 trillion shares, net income
over pretax income offered as a tax rate. The grounding guard refused each
one, correctly, and the retry gave up. The facts were exact; the model was the
calculator, and it is not one.

So the model no longer computes. It names two operands and an operation, and
this returns the result with the inputs it used, so the working can be shown
and checked.

Operands
--------
Each operand is one of:

- **A fact reference**, "TICKER:fact:fiscal_year" ("NVDA:revenue:2026"). It is
  looked up here through `get_financial_fact`, so the value is the reported
  one and cannot be mistyped. The result carries its accession number.
- **A figure a tool returned this turn**, passed as the number: a closing
  price, a row of a statement, an earlier result of this tool. The agent loop
  refuses a number that no tool result in the turn contains before this is
  called (`agent.grounding.ungrounded_operands`), because a result of this
  tool is evidence for the answer, and an invented operand would otherwise
  come out the other side looking computed.

Operations
----------
add, subtract, multiply, divide; percent_change (from a to b, in percent);
percent_of (a as a percentage of b: a share, a margin, an effective rate).
One step per call. A longer calculation is a chain: the `value` of one result
is an operand of the next.

What comes back
---------------
`value` at full precision, `formatted`, the `formula` with its numbers, both
`inputs` with their labels and sources, and `rounded`: the value written at
the coarser precisions an answer might use ("65.47%", "65.5%", "65%"). Those
are there so that an answer which rounds is quoting something this tool
returned, not rounding on its own.
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from tools.base import ERROR_BAD_INPUT, ERROR_UNSUPPORTED, error, ok
from tools.facts import get_financial_fact

__all__ = ["calculate", "Operation", "OPERATIONS", "TOOL_NAME", "parse_operand", "is_reference"]

TOOL_NAME = "calculate"

Operation = Literal["add", "subtract", "multiply", "divide", "percent_change", "percent_of"]
OPERATIONS: tuple[str, ...] = (
    "add", "subtract", "multiply", "divide", "percent_change", "percent_of",
)
_PERCENT_OPERATIONS = ("percent_change", "percent_of")

_ALIASES = {
    "+": "add", "plus": "add", "sum": "add", "addition": "add",
    "-": "subtract", "minus": "subtract", "difference": "subtract", "subtraction": "subtract",
    "*": "multiply", "x": "multiply", "times": "multiply", "product": "multiply",
    "multiplication": "multiply",
    "/": "divide", "ratio": "divide", "division": "divide", "over": "divide",
    "growth": "percent_change", "percentage_change": "percent_change",
    "pct_change": "percent_change", "growth_rate": "percent_change",
    "percentage_of": "percent_of", "share": "percent_of", "share_of": "percent_of",
    "pct_of": "percent_of", "margin": "percent_of",
}

# TICKER:fact:fiscal_year, with FY or fy allowed on the year.
_REFERENCE = re.compile(r"^\s*([A-Za-z][A-Za-z.\-]{0,9})\s*[:/]\s*([A-Za-z_&][\w &\-]*?)\s*[:/]\s*"
                        r"(?:FY)?\s*(\d{4})\s*$", re.I)
_SCALE_WORDS = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "mm": 1e6,
                "billion": 1e9, "bn": 1e9, "b": 1e9, "trillion": 1e12, "tn": 1e12, "t": 1e12}
_NUMBER = re.compile(r"^\s*\(?\s*([-+]?)\s*\$?\s*([\d,]*\.?\d+(?:[eE][-+]?\d+)?)\s*"
                     r"(%|[A-Za-z]+)?\s*\)?\s*$")


def is_reference(raw: Any) -> bool:
    """Whether an operand is a fact reference and not a number."""
    return isinstance(raw, str) and bool(_REFERENCE.match(raw))


def parse_operand(raw: Any) -> float | None:
    """The number an operand states, or None if it is not a number.

    Accepts what a model sends: 215938000000, "215,938", "$4.90", "71.07%",
    "215.9 billion". A percent sign is dropped, not applied: "71.07%" is 71.07.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(raw) else None
    if not isinstance(raw, str):
        return None
    match = _NUMBER.match(raw)
    if not match:
        return None
    sign, digits, unit = match.groups()
    try:
        value = float(digits.replace(",", ""))
    except ValueError:
        return None
    if unit and unit != "%":
        scale = _SCALE_WORDS.get(unit.lower())
        if scale is None:
            return None
        value *= scale
    return -value if sign == "-" else value


def _plain(value: float) -> str:
    """A number written out, without float noise."""
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):,}"
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def _roundings(value: float, percent: bool) -> dict[str, str]:
    """The value at the coarser precisions an answer might quote.

    Only roundings that keep at least two significant digits: "0" for 0.07 is
    not a rounding anyone should quote.
    """
    out: dict[str, str] = {}
    suffix = "%" if percent else ""
    magnitude = abs(value)
    scales = [("", 1.0)]
    if not percent and magnitude >= 1e6:
        scales = [(" trillion", 1e12), (" billion", 1e9), (" million", 1e6)]
    for word, scale in scales:
        scaled = value / scale
        if word and not (1 <= abs(scaled) < 100_000):
            continue
        for places in (2, 1, 0):
            text = f"{scaled:,.{places}f}"
            if len(text.replace(",", "").replace(".", "").replace("-", "").lstrip("0")) < 2:
                continue
            out[f"{places}dp{word.replace(' ', '_')}"] = f"{text}{word}{suffix}"
    return out


async def _resolve(raw: Any, label: str, name: str) -> tuple[dict[str, Any] | None, dict | None]:
    """(operand, None) or (None, error envelope)."""
    if is_reference(raw):
        ticker, fact, year = _REFERENCE.match(raw).groups()
        found = await get_financial_fact(ticker, fact.strip(), int(year))
        if not found.get("ok"):
            return None, error(
                found.get("error", ERROR_BAD_INPUT),
                f"Operand {name} ({raw!r}) could not be looked up: {found.get('message')}",
            )
        if "value" not in found or found.get("ratio"):
            return None, error(
                ERROR_BAD_INPUT,
                f"Operand {name} ({raw!r}) names a ratio, not a reported figure. Use "
                f"calculate_ratio for it, or pass the two facts it is made of.",
            )
        return {
            "label": label or f"{found['ticker']} fiscal {found['fiscal_year']} {found['fact']}",
            "value": found["value"], "unit": found.get("unit"),
            "source": "get_financial_fact", "reference": raw.strip(),
            "accession": found.get("accession"), "period_end": found.get("period_end"),
        }, None
    value = parse_operand(raw)
    if value is None:
        return None, error(
            ERROR_BAD_INPUT,
            f"Operand {name} must be a number a tool returned this turn, or a fact "
            f"reference written TICKER:fact:fiscal_year such as 'NVDA:revenue:2026'. "
            f"Got {raw!r}.",
        )
    return {"label": label or name, "value": value, "source": "a tool result from this turn"}, None


async def calculate(
    operation: Operation,
    a: float | str,
    b: float | str,
    a_label: str = "",
    b_label: str = "",
) -> dict[str, Any]:
    """Do one step of arithmetic on two figures and return the result with its inputs.

    Use this for EVERY figure you compute: a growth rate, a share of revenue, a
    margin or tax rate from two line items, a market value from shares and a
    price, a P/E from a price and EPS, a difference between two figures. Never
    do arithmetic yourself, not even a subtraction: a figure in your answer
    that no tool returned is discarded. For a longer calculation, call this
    again with the previous result's "value" as an operand.

    Args:
        operation: "add" (a + b), "subtract" (a - b), "multiply" (a * b),
            "divide" (a / b), "percent_change" (the change from a to b, as a
            percentage of a: a is the earlier figure), or "percent_of" (a as a
            percentage of b: a share, a margin, an effective tax rate).
        a: The first operand. Either a fact reference written
            TICKER:fact:fiscal_year ("NVDA:revenue:2026", using the fact names
            of get_financial_fact), which is looked up for you, or a number
            exactly as a tool returned it this turn (a closing price, a
            statement row, the "value" of an earlier calculate result). A
            number no tool returned is refused.
        b: The second operand, in the same forms.
        a_label: What a is, for a number ("NVDA close on 2026-08-07").
        b_label: What b is, for a number.

    Returns:
        A dict with "ok": True and "value" (full precision; a percentage for
        the two percent operations), "formatted", "formula" (the arithmetic
        with its numbers), "inputs" (each operand's label, value and source,
        with the accession number for a fact reference), and "rounded" (the
        value at coarser precisions; quote one of these if you round). Both
        operands must be in the same units for add and subtract. On failure,
        {"ok": False, "error": code, "message": ...}.
    """
    name = _ALIASES.get((operation or "").strip().lower().replace(" ", "_"),
                        (operation or "").strip().lower().replace(" ", "_"))
    if name not in OPERATIONS:
        return error(ERROR_UNSUPPORTED,
                     f"Unknown operation {operation!r}. Supported: {', '.join(OPERATIONS)}.")

    first, failed = await _resolve(a, a_label, "a")
    if failed:
        return failed
    second, failed = await _resolve(b, b_label, "b")
    if failed:
        return failed
    x, y = float(first["value"]), float(second["value"])

    if name in ("divide", "percent_of") and y == 0:
        return error(ERROR_BAD_INPUT, f"b ({second['label']}) is zero, so {name} is undefined.")
    if name == "percent_change" and x == 0:
        return error(ERROR_BAD_INPUT,
                     f"a ({first['label']}) is zero, so a percentage change from it is undefined.")

    if name == "add":
        value, formula = x + y, f"{_plain(x)} + {_plain(y)}"
    elif name == "subtract":
        value, formula = x - y, f"{_plain(x)} - {_plain(y)}"
    elif name == "multiply":
        value, formula = x * y, f"{_plain(x)} * {_plain(y)}"
    elif name == "divide":
        value, formula = x / y, f"{_plain(x)} / {_plain(y)}"
    elif name == "percent_change":
        value, formula = (y - x) / abs(x) * 100, f"({_plain(y)} - {_plain(x)}) / {_plain(abs(x))} * 100"
    else:
        value, formula = x / y * 100, f"{_plain(x)} / {_plain(y)} * 100"

    percent = name in _PERCENT_OPERATIONS
    value = round(value, 6)
    return ok(
        operation=name,
        value=value,
        formatted=f"{value:,.2f}%" if percent else _plain(round(value, 4)),
        unit="percent" if percent else None,
        formula=f"{formula} = {_plain(value)}{'%' if percent else ''}",
        inputs={"a": first, "b": second},
        rounded=_roundings(value, percent),
        source="calculate",
    )
