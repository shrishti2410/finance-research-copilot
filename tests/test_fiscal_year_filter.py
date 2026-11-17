"""Retrieval filters on the fiscal year a question names.

With three 10-Ks per company indexed, a question about fiscal 2026 came back
with the 2024 and 2025 filings. Offline: the search is stubbed, so this pins
what the retriever asks for, not what the index holds.
"""

from __future__ import annotations

import asyncio

import pytest

from rag import retriever


@pytest.mark.parametrize("query, expected", [
    ("How much did NVIDIA spend on R&D in fiscal 2026?", ["FY2026", "FY2027", "FY2028"]),
    ("What was revenue in fiscal year 2024?", ["FY2024", "FY2025", "FY2026"]),
    ("Apple's FY2023 net income", ["FY2023", "FY2024", "FY2025"]),
    ("Apple's FY 2023 net income", ["FY2023", "FY2024", "FY2025"]),
    ("growth from fiscal 2025 to fiscal 2026", ["FY2026", "FY2027", "FY2028"]),
])
def test_a_named_fiscal_year_selects_the_filings_that_report_it(query, expected):
    """A 10-K carries its own year and the two before it, so FY2023's figures
    are in the FY2023, FY2024 and FY2025 filings."""
    assert retriever.fiscal_periods_named(query) == expected


@pytest.mark.parametrize("query", [
    "What did Apple raise its quarterly dividend to in May 2025?",
    "How large was the buyback announced in 2025?",
    "What are NVIDIA's main risks?",
    "", "fiscal 26",
])
def test_a_calendar_date_or_no_year_sets_no_filter(query):
    assert retriever.fiscal_periods_named(query) is None


def _run_with(monkeypatch, results_by_filter):
    calls = []

    async def fake_search(conn, vector, **kwargs):
        calls.append(kwargs.get("fiscal_period"))
        key = tuple(kwargs["fiscal_period"]) if kwargs.get("fiscal_period") else None
        return results_by_filter.get(key, [])

    class _Conn:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(retriever, "search", fake_search)
    monkeypatch.setattr(retriever, "embed_query", lambda query, model: [0.0] * 384)
    monkeypatch.setattr(retriever, "connection", lambda: _Conn())
    monkeypatch.setattr(retriever, "rerank", lambda hits, intent, k: hits[:k])
    return calls


def test_the_filter_is_passed_to_the_search(monkeypatch):
    calls = _run_with(monkeypatch, {("FY2026", "FY2027", "FY2028"): ["hit"]})
    hits = asyncio.run(retriever.search_filings("NVIDIA R&D in fiscal 2026"))
    assert hits == ["hit"] and calls == [["FY2026", "FY2027", "FY2028"]]


def test_a_year_with_nothing_indexed_falls_back_to_every_year(monkeypatch):
    calls = _run_with(monkeypatch, {None: ["any year"]})
    hits = asyncio.run(retriever.search_filings("NVIDIA R&D in fiscal 2031"))
    assert hits == ["any year"]
    assert calls == [["FY2031", "FY2032", "FY2033"], None]


def test_a_period_the_caller_passes_wins_over_the_question(monkeypatch):
    calls = _run_with(monkeypatch, {("FY2024",): ["hit"]})
    asyncio.run(retriever.search_filings("revenue in fiscal 2026", fiscal_period="FY2024"))
    assert calls == ["FY2024"]


def test_a_question_without_a_fiscal_year_is_not_filtered(monkeypatch):
    calls = _run_with(monkeypatch, {None: ["hit"]})
    asyncio.run(retriever.search_filings("Apple's dividend in May 2025"))
    assert calls == [None]
