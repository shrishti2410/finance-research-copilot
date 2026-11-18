"""Answers cite sources in plain words, never internal field names or JSON
paths (live-site feedback, 2026-11-18: "snapshot revenue_growth: 1.191069",
"signal.backtest_statement").

Two layers: the system prompt says how to cite, and plain_labels() turns any
field name left in the text into words after the guards have run. It only
rewrites identifiers, never a figure, and leaves the "Final figure:" line as
the model wrote it (the grader reads that line).
"""

from __future__ import annotations

import re

import pytest

from agent.orchestrator import plain_labels
from agent.prompts import system_prompt

PROMPT = system_prompt(5)


def test_the_prompt_says_how_to_cite_a_source():
    assert "plain words" in PROMPT
    assert "Yahoo Finance" in PROMPT and "SEC 10-K" in PROMPT
    assert re.search(r"never .*field name", PROMPT, re.I)


@pytest.mark.parametrize("raw, plain", [
    ("Revenue grew 119.11% (snapshot revenue_growth).", "Revenue grew 119.11% (snapshot revenue growth)."),
    ("As signal.backtest_statement says, there is no edge.", "As signal backtest statement says, there is no edge."),
    ("From get_company_snapshot: net margin 2.96%.", "From get company snapshot: net margin 2.96%."),
    ("See `financials.years[].net_margin`.", "See financials years net margin."),
])
def test_field_names_become_words(raw, plain):
    assert plain_labels(raw) == plain


@pytest.mark.parametrize("text", [
    "NVIDIA's fiscal 2026 revenue was $215.938 billion (SEC 10-K).",
    "Source: finance.yahoo.com, SBIN.NS and DIXON.NS on the NSE.",
    "The S&P 500 (^GSPC) fell 1.2%; e.g. on 2026-11-17.",
])
def test_ordinary_text_is_untouched(text):
    assert plain_labels(text) == text


def test_figures_and_the_final_figure_line_are_never_changed():
    text = "Net margin 2.96% (net_margin 0.02964).\nFinal figure: 2.96%"
    out = plain_labels(text)
    assert re.findall(r"\d+(?:\.\d+)?", out) == re.findall(r"\d+(?:\.\d+)?", text)
    assert out.endswith("Final figure: 2.96%")
