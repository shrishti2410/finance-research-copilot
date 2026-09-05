"""Retrieval quality against the real indexed filings.

Separate from tests/test_retrieval.py on purpose. That module inserts synthetic
chunks to exercise the mechanics -- including one whose content is
"Revenue 100 Cost of revenue 40 Gross profit 60" and whose content_type is
"table". Under the figure-query boost that fixture outranks the actual income
statements, so a quality test sharing a database with it measures the fixture.

These run against whatever `scripts/index_filings.py` last wrote and skip when
NVDA and AAPL are not both present. They are the regression guard for the audit
finding that "What was total revenue?" returned two passages discussing revenue
without stating it, with NVDA's statement at rank 5 and Apple's at rank 13.
"""

import asyncio

import pytest

from rag import vector_store as vs
from rag.retriever import search_filings

_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def close_loop():
    yield
    from db.base import engine
    _LOOP.run_until_complete(engine.dispose())
    _LOOP.close()


@pytest.fixture(scope="module")
def real_index():
    """The real NVDA and AAPL filings. Skips if the index is not populated."""
    async def go():
        async with vs.connection() as conn:
            return await vs.stats(conn)

    try:
        info = run(go())
    except Exception as exc:  # noqa: BLE001 - unreachable database, missing table
        pytest.skip(f"vector store unavailable: {type(exc).__name__}")

    tickers = {doc["ticker"] for doc in info["documents"]}
    if not {"NVDA", "AAPL"} <= tickers:
        pytest.skip("NVDA and AAPL are not indexed; run scripts/index_filings.py")
    if any(doc["ticker"].startswith("ZZ") for doc in info["documents"]):
        pytest.skip("synthetic fixture chunks are in the index; run this module alone")
    return info


def search(query: str, **kwargs):
    return run(search_filings(query, **kwargs))


def test_income_statements_outrank_prose_for_a_figure_query(real_index):
    """The audit finding this fixes: 'What was total revenue?' used to return
    two passages that discuss revenue without stating it, with NVDA's statement
    at rank 5 and Apple's at rank 13."""
    hits = search("What was total revenue?", k=3)
    tables = [h for h in hits[:3] if h.content_type == "table"]

    assert len(tables) >= 2, [f"{h.content_type}:{h.ticker}" for h in hits]
    assert {h.ticker for h in tables} >= {"NVDA", "AAPL"}


def test_the_top_hit_for_a_figure_query_actually_contains_the_figure(real_index):
    """Rank is not the point; containing the answer is."""
    top = search("What was total revenue?", k=1)[0]
    assert "215,938" in top.content or "416,161" in top.content


def test_an_explanatory_query_still_returns_prose(real_index):
    """The margin explanation lives in MD&A. Boosting tables for every query
    mentioning 'margin' would bury it."""
    hits = search("Why did gross margin decline?", k=3)
    assert all(h.content_type == "text" for h in hits)
    assert all(h.boost == 0.0 for h in hits)


def test_a_risk_query_is_unaffected_by_the_rerank(real_index):
    """Regression guard: this query has no statement terms and must rank purely
    on the encoder."""
    with_rerank = search("What are the main competitive risks?", k=3)
    without = run(search_filings("What are the main competitive risks?", k=3,
                                 rerank_results=False))

    assert [h.chunk_id for h in with_rerank] == [h.chunk_id for h in without]
    assert all(h.content_type == "text" for h in with_rerank)
