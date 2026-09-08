"""Evaluation question sets and ground truth.

    from eval.datasets import load_cases, filings_only, tool_only, mixed

    for case in load_cases():
        answer = ask(case.question)
        print(case.id, case.matches(extract_number(answer)))

Every case has a numeric answer, because a numeric answer can be scored without
a second model in the loop. "Is this paragraph faithful?" needs a judge, and a
judge is another thing that can be wrong; "is 71.07 within 0.5 of 71.07" does
not.

Where the numbers come from
---------------------------
None of them were typed by hand. `questions.json` is generated: line items are
read out of the parsed statements already in Postgres, tool answers come from
calling the tools, and derived figures are computed from those. Each case
carries a `source` naming exactly where its number came from, so a failing case
can be argued with rather than trusted.

What this set does not cover
----------------------------
Only what the corpus actually holds -- NVDA's FY2026 10-K and AAPL's FY2025,
and within those only Item 1A, Item 7, and the income statement. There are no
questions about the balance sheet or cash flow statement *from filings*,
because those sections are not ingested. Two cases deliberately ask for
balance-sheet ratios that are reachable **only** through `calculate_ratio`, and
they are labelled that way: a system that answers them from "filings" is
hallucinating, and the set should be able to catch that.

Nor does it cover the things a numeric answer cannot express: whether a risk
factor was summarised fairly, whether a causal claim was sourced. Those need a
different harness.

Answers that move
-----------------
Ratios come from closed fiscal years and are stable. Prices come from closed
historical windows, but yfinance re-adjusts past closes for dividends, so an
Apple close drifts by roughly the dividend yield per year -- which is why price
tolerances are 2% rather than a cent. `pe_ratio` is excluded entirely: it moves
with the live share price, so no fixed expected value can be honest about it.
Two cases compute a P/E from a filing EPS and a *historical* close instead,
which is stable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

__all__ = [
    "Case", "load_cases", "load_dataset", "filings_only", "tool_only",
    "mixed", "by_company", "QUESTIONS_PATH",
]

QUESTIONS_PATH = Path(__file__).with_name("questions.json")

# Tools a case may name. Kept here so a typo in the dataset is a load error
# rather than a case that silently never matches a filter.
KNOWN_TOOLS = frozenset({"search_filings", "calculate_ratio", "get_stock_price"})


@dataclass(frozen=True)
class Case:
    """One question with a checkable numeric answer."""

    id: str
    question: str
    expected_answer: float
    unit: str
    tolerance: float
    answerable_via: tuple[str, ...]
    company: str
    source: str
    notes: str = ""

    def matches(self, value: float | None) -> bool:
        """Whether `value` is within tolerance of the expected answer.

        Tolerance is absolute and in `unit`; for a percent unit that means
        percentage points. None -- no number found in the answer -- is a miss,
        never a pass.
        """
        if value is None:
            return False
        return abs(value - self.expected_answer) <= self.tolerance

    @property
    def needs_filings(self) -> bool:
        return "search_filings" in self.answerable_via

    @property
    def needs_tools(self) -> bool:
        return bool(set(self.answerable_via) - {"search_filings"})


def load_dataset(path: Path | str = QUESTIONS_PATH) -> dict:
    """The raw file, including its provenance header."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def load_cases(path: Path | str = QUESTIONS_PATH) -> tuple[Case, ...]:
    """Every case, validated.

    Validation is strict on purpose: a dataset with a duplicate id, an unknown
    tool name or a non-positive tolerance will produce results that look fine
    and mean nothing.
    """
    payload = load_dataset(path)
    cases: list[Case] = []
    seen: set[str] = set()

    for raw in payload["cases"]:
        case_id = raw["id"]
        if case_id in seen:
            raise ValueError(f"duplicate case id: {case_id}")
        seen.add(case_id)

        via = tuple(raw["answerable_via"])
        unknown = set(via) - KNOWN_TOOLS
        if unknown:
            raise ValueError(f"{case_id}: unknown tool(s) {sorted(unknown)}")
        if not via:
            raise ValueError(f"{case_id}: answerable_via must name at least one tool")
        if raw["tolerance"] <= 0:
            raise ValueError(
                f"{case_id}: tolerance must be positive; an exact-equality case "
                f"cannot pass against a model that writes 71.07% as 71.1%"
            )

        cases.append(Case(
            id=case_id,
            question=raw["question"],
            expected_answer=float(raw["expected_answer"]),
            unit=raw["unit"],
            tolerance=float(raw["tolerance"]),
            answerable_via=via,
            company=raw["company"],
            source=raw["source"],
            notes=raw.get("notes", ""),
        ))
    return tuple(cases)


def filings_only(cases=None) -> tuple[Case, ...]:
    """Answerable from the indexed 10-Ks alone."""
    return tuple(c for c in (cases or load_cases())
                 if c.answerable_via == ("search_filings",))


def tool_only(cases=None) -> tuple[Case, ...]:
    """Answerable from live tools alone, with no filing needed."""
    return tuple(c for c in (cases or load_cases()) if not c.needs_filings)


def mixed(cases=None) -> tuple[Case, ...]:
    """Needs a filing *and* a tool, or is reachable by either route."""
    return tuple(c for c in (cases or load_cases())
                 if c.needs_filings and c.needs_tools)


def by_company(company: str, cases=None) -> tuple[Case, ...]:
    return tuple(c for c in (cases or load_cases()) if company in c.company)
