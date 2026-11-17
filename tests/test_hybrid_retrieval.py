"""Hybrid retrieval: full-text search and Reciprocal Rank Fusion.

The fusion and the query builder need nothing. The search tests run against
the Postgres in DATABASE_URL with the corpus indexed and migration 0009
applied, and skip otherwise.
"""

from __future__ import annotations

import asyncio

import pytest

from rag.retriever import RRF_K, reciprocal_rank_fusion
from rag.vector_store import SearchHit, _or_query


def _hit(cid, score=0.0):
    return SearchHit(score=score, chunk_id=cid, content="", company="", ticker="", section="",
                     fiscal_period="", chunk_index=0, content_type="text", filing_type="10-K",
                     source_url="", token_count=0)


def test_rrf_sums_reciprocal_ranks_and_orders_by_them():
    a, b, c, d = (_hit(x) for x in "abcd")
    fused = reciprocal_rank_fusion([[a, b, c], [c, a, d]], k=4)
    expected = {"a": 1 / (RRF_K + 1) + 1 / (RRF_K + 2), "b": 1 / (RRF_K + 2),
                "c": 1 / (RRF_K + 3) + 1 / (RRF_K + 1), "d": 1 / (RRF_K + 3)}
    assert [h.chunk_id for h in fused] == ["a", "c", "b", "d"]
    for h in fused:
        assert h.score == pytest.approx(expected[h.chunk_id])


def test_rrf_keeps_the_first_rankings_hit_and_cuts_at_k():
    first = _hit("x", score=0.9)
    first = first.__class__(**{**first.__dict__, "vector_score": 0.9})
    fused = reciprocal_rank_fusion([[first], [_hit("x", score=12.0), _hit("y")]], k=1)
    assert len(fused) == 1 and fused[0].chunk_id == "x" and fused[0].vector_score == 0.9


def test_the_text_query_is_words_joined_by_or_and_nothing_else():
    assert _or_query("What did Apple raise its quarterly dividend to in May 2025?") == \
        "what | did | apple | raise | its | quarterly | dividend | to | in | may | 2025"
    assert _or_query("R&D: 'x' | !(drop) <-> table;") == "drop | table"
    assert _or_query("?? !") == ""


# --- against the database ----------------------------------------------------------

def _db(work):
    from db.base import engine
    from rag.vector_store import connection

    async def go():
        try:
            async with connection() as conn:
                return await work(conn)
        finally:
            await engine.dispose()

    try:
        return asyncio.run(go())
    except Exception as exc:  # noqa: BLE001
        if "content_tsv" in str(exc) or type(exc).__name__ in ("OSError", "ConnectionRefusedError"):
            pytest.skip(f"full-text column not available ({type(exc).__name__}); migrate to 0009")
        raise


def test_full_text_search_finds_the_words_and_respects_filters():
    from rag.vector_store import text_search

    hits = _db(lambda conn: text_search(conn, "quarterly dividend", 10, company="AAPL"))
    if not hits:
        pytest.skip("no Apple chunks indexed")
    assert all(h.ticker == "AAPL" for h in hits)
    # OR semantics: every hit has one of the words; the best has both.
    assert all(("dividend" in h.content.lower()) or ("quarter" in h.content.lower()) for h in hits)
    assert "dividend" in hits[0].content.lower() and "quarter" in hits[0].content.lower()
    ranks = [h.score for h in hits]
    assert ranks == sorted(ranks, reverse=True) and all(h.vector_score == 0.0 for h in hits)


def test_hybrid_search_returns_fused_hits_under_the_same_filters():
    try:
        from rag.embeddings import embed_query
        embed_query("warm-up")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"bge-small unavailable: {type(exc).__name__}")
    from rag.retriever import search_filings

    async def work(conn):
        plain = await search_filings("Apple quarterly dividend", company="AAPL", k=5,
                                     conn=conn, hybrid=False)
        fused = await search_filings("Apple quarterly dividend", company="AAPL", k=5,
                                     conn=conn, hybrid=True)
        return plain, fused

    plain, fused = _db(work)
    if not plain:
        pytest.skip("no Apple chunks indexed")
    assert len(fused) == 5 and all(h.ticker == "AAPL" for h in fused)
    scores = [h.score for h in fused]
    assert scores == sorted(scores, reverse=True)
    assert all(s <= 2 / (RRF_K + 1) + 1e-12 for s in scores)   # RRF scores, two rankings


# --- the company a question names ----------------------------------------------------

@pytest.mark.parametrize("question, tickers", [
    ("How large was the share repurchase program Apple announced in May 2025?", ["AAPL"]),
    ("Compare NVIDIA's gross margin to Apple's.", ["AAPL", "NVDA"]),
    ("What was JPMorgan Chase's net income?", ["JPM"]),
    ("What did Coca-Cola pay in dividends?", ["KO"]),
    ("What is MSFT's operating margin?", ["MSFT"]),
    ("What is the gross margin?", []),
    ("Is a V-shaped recovery likely?", []),            # single-letter ticker: never from "V"
    ("The pineapple harvest", []),                      # "apple" inside a word
])
def test_companies_named_reads_names_and_tickers_but_not_lookalikes(question, tickers):
    from rag.retriever import companies_named
    assert companies_named(question) == tickers


def test_every_indexed_company_has_aliases():
    from ingestion.corpus import CORPUS_TICKERS
    from rag.retriever import COMPANY_ALIASES
    assert set(CORPUS_TICKERS) == set(COMPANY_ALIASES)


def test_an_inferred_company_filters_and_a_callers_company_wins():
    try:
        from rag.embeddings import embed_query
        embed_query("warm-up")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"bge-small unavailable: {type(exc).__name__}")
    from rag.retriever import search_filings

    async def work(conn):
        inferred = await search_filings("Apple share repurchase program", k=5, conn=conn,
                                        hybrid=False, infer_company=True)
        overridden = await search_filings("Apple share repurchase program", company="NVDA",
                                          k=5, conn=conn, hybrid=False, infer_company=True)
        return inferred, overridden

    inferred, overridden = _db(work)
    if not inferred:
        pytest.skip("corpus not indexed")
    assert {h.ticker for h in inferred} == {"AAPL"}
    assert {h.ticker for h in overridden} == {"NVDA"}
