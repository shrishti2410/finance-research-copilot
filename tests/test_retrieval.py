"""Tests for the vector store and the retriever.

The SQL-building helpers are tested directly, with no database. The rest runs
against the live Postgres in DATABASE_URL, migrated to head, and skips with a
clear message when it is unreachable -- so it never passes silently.

Database tests write under a reserved accession and delete it afterwards, so
they cannot disturb the indexed filings sitting in the same table.
"""

import asyncio

import numpy as np
import pytest

from ingestion.chunker import Chunk, ChunkMetadata
from rag import vector_store as vs
from rag.vector_store import SearchHit
from rag.retriever import (
    LEXICAL_WEIGHT,
    TABLE_BOOST,
    query_intent,
    rerank,
    resolve_section,
    search_filings,
)

TEST_ACCESSION = "TEST-0000000000-00-000000"


# One event loop for the whole module. `asyncio.run` makes a fresh loop per
# call, but db.base.engine pools connections bound to the loop that opened them,
# so the second call would find a connection whose loop is already closed.
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def close_loop():
    yield
    from db.base import engine
    _LOOP.run_until_complete(engine.dispose())
    _LOOP.close()


# ─────────────────────────────────────────────────────────────────────────────
# SQL construction -- no database needed
# ─────────────────────────────────────────────────────────────────────────────

def test_array_backend_binds_a_list_not_a_string():
    """asyncpg types a parameter from the SQL, not the value: a `real[]` cast
    with a string bound to it is a DataError, not a coercion."""
    bound = vs._bind([0.5, -0.25], vs.BACKEND_ARRAY)
    assert isinstance(bound, list) and bound == [0.5, -0.25]


def test_pgvector_backend_binds_its_text_form():
    """asyncpg has no codec for pgvector's binary format, so the value goes in
    as text and Postgres converts -- which is why _expr casts through text."""
    assert vs._bind([0.5, -0.25], vs.BACKEND_PGVECTOR) == "[0.5,-0.25]"
    assert "AS text" in vs._expr("qvec", vs.BACKEND_PGVECTOR)


def test_expr_casts_to_the_column_type_for_each_backend():
    assert vs._expr("qvec", vs.BACKEND_PGVECTOR).endswith("AS vector)")
    assert vs._expr("qvec", vs.BACKEND_ARRAY) == "CAST(:qvec AS real[])"


@pytest.mark.parametrize(
    "shorthand,expected",
    [
        ("risk", "Item 1A"), ("Risk Factors", "Item 1A"), ("1a", "Item 1A"),
        ("mdna", "Item 7"), ("MD&A", "Item 7"), ("7", "Item 7"),
        ("income", "Income Statement"), ("table", "Income Statement"),
        (None, None),
    ],
)
def test_resolve_section_maps_shorthand(shorthand, expected):
    assert resolve_section(shorthand) == expected


def test_unknown_section_passes_through_as_a_literal():
    """Rejecting it would break the moment another Item is indexed."""
    assert resolve_section("Item 9A") == "Item 9A"


def test_empty_query_is_rejected():
    """An empty string embeds fine and returns confident nonsense."""
    with pytest.raises(ValueError):
        run(search_filings("   "))


# ─────────────────────────────────────────────────────────────────────────────
# Against the live database
# ─────────────────────────────────────────────────────────────────────────────

def make_chunk(index: int, text: str, *, ticker: str, company: str,
               section: str, content_type: str = "text") -> Chunk:
    return Chunk(
        chunk_id=f"{TEST_ACCESSION}:{section}:{index}",
        content=text,
        token_count=len(text.split()),
        metadata=ChunkMetadata(
            company=company, filing_type="10-K", fiscal_period="FY2026",
            section=section, chunk_index=index, ticker=ticker, cik="0000000000",
            accession=TEST_ACCESSION, period_end="2026-01-25",
            filing_date="2026-02-25", source_url="https://example.invalid/x.htm",
            content_type=content_type,
        ),
    )


FIXTURE_CHUNKS = [
    make_chunk(0, "Competition from rival chipmakers could reduce our market share.",
               ticker="ZZTEST", company="Ziggurat Test Corp", section="Item 1A - Risk Factors"),
    make_chunk(1, "Revenue grew because datacenter demand grew.",
               ticker="ZZTEST", company="Ziggurat Test Corp",
               section="Item 7 - Management's Discussion and Analysis"),
    make_chunk(2, "Revenue 100 Cost of revenue 40 Gross profit 60",
               ticker="ZZOTHER", company="Other Test Inc", section="Income Statement",
               content_type="table"),
]


@pytest.fixture(scope="module")
def indexed():
    """Insert the fixture chunks with real embeddings; remove them afterwards."""
    try:
        from rag.embeddings import embed_passages, get_encoder
        get_encoder()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"bge-small unavailable: {type(exc).__name__}")

    vectors = embed_passages([c.content for c in FIXTURE_CHUNKS])

    async def setup():
        async with vs.connection() as conn:
            await vs.upsert_chunks(conn, FIXTURE_CHUNKS, vectors)

    async def teardown():
        async with vs.connection() as conn:
            await vs.delete_accession(conn, TEST_ACCESSION)

    try:
        run(setup())
    except vs.VectorStoreNotReady as exc:
        pytest.skip(str(exc))
    except Exception as exc:  # noqa: BLE001 - connection refused, auth, ...
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")

    yield
    run(teardown())


def search(query: str, **kwargs):
    return run(search_filings(query, **kwargs))


def test_backend_is_one_of_the_two_supported(indexed):
    async def go():
        async with vs.connection() as conn:
            return await vs.detect_backend(conn)
    assert run(go()) in (vs.BACKEND_PGVECTOR, vs.BACKEND_ARRAY)


def test_a_chunk_retrieves_itself_first(indexed):
    """End-to-end: chunk -> embedding -> storage -> query -> ranking. If any
    stage mangles the vector, the nearest neighbour of a passage stops being
    the passage."""
    hits = search(FIXTURE_CHUNKS[0].content, k=1)
    assert hits[0].chunk_id == FIXTURE_CHUNKS[0].chunk_id


def test_scores_are_cosine_similarities_in_range(indexed):
    for hit in search("competition", k=5):
        assert -1.0 <= hit.score <= 1.0


def test_results_are_ordered_by_descending_score(indexed):
    scores = [hit.score for hit in search("revenue growth", k=5)]
    assert scores == sorted(scores, reverse=True)


def test_company_filter_restricts_results(indexed):
    hits = search("revenue", company="ZZTEST", k=10)
    assert hits
    assert {hit.ticker for hit in hits} == {"ZZTEST"}


def test_company_filter_accepts_a_name_substring(indexed):
    """Callers say 'NVDA', 'NVIDIA' or 'NVIDIA CORP'; all three must work."""
    hits = search("revenue", company="Ziggurat", k=10)
    assert hits and {hit.ticker for hit in hits} == {"ZZTEST"}


def test_section_filter_restricts_results(indexed):
    hits = search("anything at all", section="risk", company="ZZTEST", k=10)
    assert hits
    assert all(hit.section.startswith("Item 1A") for hit in hits)


def test_filters_are_applied_before_the_top_k(indexed):
    """Post-filtering a global top-k for one company routinely returns nothing.
    A k=1 search restricted to a company must still return that company's best
    passage, not an empty list."""
    hits = search("competition", company="ZZTEST", k=1)
    assert len(hits) == 1 and hits[0].ticker == "ZZTEST"


def test_content_type_filter_reaches_the_table_chunk(indexed):
    """The escape hatch for the known weakness: a table chunk loses to prose on
    a conversational query even when it holds the answer."""
    hits = search("total revenue", content_type="table", k=5)
    assert hits and all(hit.content_type == "table" for hit in hits)


def test_a_filter_matching_nothing_returns_nothing(indexed):
    assert search("revenue", company="NoSuchCompanyAnywhere", k=5) == []


def test_min_score_drops_weak_hits(indexed):
    assert search("revenue", k=10, min_score=1.01) == []


def test_reindexing_updates_in_place(indexed):
    """Chunk ids are stable, so a rerun after a chunker change must not leave
    two generations of the same passage competing for the top-k."""
    from rag.embeddings import embed_passages

    async def go():
        async with vs.connection() as conn:
            before = await _count(conn)
            await vs.upsert_chunks(
                conn, FIXTURE_CHUNKS, embed_passages([c.content for c in FIXTURE_CHUNKS])
            )
            return before, await _count(conn)

    before, after = run(go())
    assert before == after == len(FIXTURE_CHUNKS)


async def _count(conn) -> int:
    from sqlalchemy import text
    return (await conn.execute(
        text(f"SELECT COUNT(*) FROM {vs.TABLE} WHERE accession = :a"),
        {"a": TEST_ACCESSION},
    )).scalar_one()


def test_hits_carry_provenance_for_a_citation(indexed):
    hit = search("competition", company="ZZTEST", k=1)[0]
    assert hit.source_url.startswith("https://")
    assert "ZZTEST" in hit.citation and "10-K" in hit.citation


def test_table_hits_keep_their_structured_rows(indexed):
    """A table chunk's numbers must survive the round trip as data, not only as
    the rendered text that was embedded."""
    chunk = make_chunk(3, "Revenue 100", ticker="ZZOTHER", company="Other Test Inc",
                       section="Income Statement", content_type="table")
    object.__setattr__(chunk, "structured", {"periods": ["FY2026"],
                                             "rows": [{"label": "Revenue",
                                                       "values": [100.0]}]})

    from rag.embeddings import embed_passages

    async def go():
        async with vs.connection() as conn:
            await vs.upsert_chunks(conn, [chunk], embed_passages([chunk.content]))
            return await vs.search(conn, np.zeros(384), k=50, ticker="ZZOTHER")

    hit = next(h for h in run(go()) if h.chunk_id == chunk.chunk_id)
    assert hit.structured["rows"][0] == {"label": "Revenue", "values": [100.0]}


def test_stats_reports_the_backend_and_the_corpus(indexed):
    async def go():
        async with vs.connection() as conn:
            return await vs.stats(conn)

    info = run(go())
    assert info["backend"] in (vs.BACKEND_PGVECTOR, vs.BACKEND_ARRAY)
    assert info["total_chunks"] >= len(FIXTURE_CHUNKS)
    assert any(doc["ticker"] == "ZZTEST" for doc in info["documents"])


def test_vector_length_mismatch_is_rejected(indexed):
    """A silently wrong-length vector would be stored and then never match."""
    async def go():
        async with vs.connection() as conn:
            await vs.upsert_chunks(conn, FIXTURE_CHUNKS[:1], np.zeros((1, 7)))

    with pytest.raises(ValueError):
        run(go())


def test_chunk_and_vector_count_mismatch_is_rejected(indexed):
    async def go():
        async with vs.connection() as conn:
            await vs.upsert_chunks(conn, FIXTURE_CHUNKS, np.zeros((2, 384)))

    with pytest.raises(ValueError):
        run(go())


# ─────────────────────────────────────────────────────────────────────────────
# Hybrid re-ranking -- intent detection and scoring, no database
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "query,wants_figure",
    [
        ("What was total revenue?", True),
        ("How much net income did they report?", True),
        ("What were earnings per share?", True),
        # Explanatory: the answer is in MD&A prose, not in the grid. The grid
        # states the number and says nothing about why it moved.
        ("Why did gross margin decline?", False),
        ("What did management say about revenue growth?", False),
        ("Explain the drop in operating income", False),
        # No statement term at all.
        ("What are the main competitive risks?", False),
        ("Describe the supply chain", False),
    ],
)
def test_query_intent_decides_whether_to_prefer_a_table(query, wants_figure):
    assert query_intent(query).wants_a_figure is wants_figure


def hit(content, content_type="text", score=0.70, ticker="NVDA"):
    return SearchHit(
        score=score, vector_score=score, chunk_id=f"x:{content[:8]}", content=content,
        company="c", ticker=ticker, section="s", fiscal_period="FY2026",
        chunk_index=0, content_type=content_type, filing_type="10-K",
        source_url="https://example.invalid", token_count=10,
    )


def test_a_table_is_boosted_only_when_the_query_wants_a_figure():
    table = hit("Revenue 215,938", "table", score=0.60)
    prose = hit("Revenue grew because demand grew.", "text", score=0.65)

    figure = rerank([prose, table], query_intent("What was total revenue?"), k=2)
    assert figure[0].content_type == "table"
    assert figure[0].boost == TABLE_BOOST

    why = rerank([prose, table], query_intent("Why did revenue grow?"), k=2)
    assert why[0].content_type == "text"
    assert all(h.boost == 0.0 for h in why)


def test_no_statement_term_means_no_reordering():
    """A risk-factors query must come back exactly as the encoder ranked it."""
    hits = [hit("first", score=0.80), hit("second", score=0.70), hit("third", score=0.60)]
    ranked = rerank(hits, query_intent("What are the main competitive risks?"), k=3)

    assert [h.content for h in ranked] == ["first", "second", "third"]
    assert [h.score for h in ranked] == [0.80, 0.70, 0.60]


def test_lexical_score_is_the_share_of_query_terms_present():
    intent = query_intent("What were net income, gross profit and revenue?")
    both = hit("net income and gross profit and revenue all appear")
    one = hit("only revenue appears here")

    ranked = {h.content: h for h in rerank([both, one], intent, k=2)}
    assert ranked[both.content].lexical_score == 1.0
    assert 0 < ranked[one.content].lexical_score < 1.0


def test_the_breakdown_adds_up_to_the_reported_score():
    """The ranking has to be explainable, not just produced."""
    for h in rerank([hit("Revenue 215,938", "table", score=0.6)],
                    query_intent("What was total revenue?"), k=1):
        assert h.score == pytest.approx(
            h.vector_score + LEXICAL_WEIGHT * h.lexical_score + h.boost
        )


def test_weights_are_small_enough_not_to_overrule_the_encoder():
    """A boost that can leapfrog any gap makes the vector score decorative."""
    assert LEXICAL_WEIGHT + TABLE_BOOST < 0.20


def test_rerank_returns_at_most_k():
    hits = [hit(f"passage {i}", score=0.9 - i / 100) for i in range(20)]
    assert len(rerank(hits, query_intent("What was revenue?"), k=3)) == 3
