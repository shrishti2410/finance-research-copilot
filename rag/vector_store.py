"""Chunk + embedding storage, in the same Postgres the rest of the app uses.

One table, `filing_chunks`, holding the chunk text, its metadata, and its
384-dimensional embedding. No separate vector database: at this corpus size a
second service would be pure operational cost, and keeping vectors next to the
relational data means a metadata filter is a WHERE clause rather than a
post-filter over an approximate result set.

pgvector only
=============
The embedding column is `vector(384)` from **pgvector**, searched by cosine
distance through an HNSW index (migrations 0002 and 0005).

Until 2026-11-16 a server without pgvector got a plain `real[]` column and a
dot product computed in SQL: a sequential scan, fine for the 113 chunks of two
filings and wrong for the 15-company corpus of step 4. That fallback is gone
from this path. A `real[]` column is now `VectorStoreNotReady` at the first
search, with the fix in the message, rather than a quietly slower answer.
Migration 0005 converts an existing `real[]` column where pgvector can be
installed, and refuses where it cannot.

HNSW and filters
----------------
An approximate index scan reads `hnsw.ef_search` candidates (40 by default)
and *then* applies the WHERE clause. With 15 companies a filter on one of them
keeps about one candidate in fifteen, so a k=30 search could come back with two
rows. pgvector 0.8's iterative scan keeps reading the graph until the filter is
satisfied; `search` turns it on for its own transaction (`SET LOCAL`).
`scripts/retrieval_latency.py` measures what the index returns against an
exact scan.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np
from sqlalchemy import text

from db.base import engine
from rag.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL

TABLE = "filing_chunks"

BACKEND_PGVECTOR = "pgvector"

# How many candidates an HNSW scan keeps while searching. pgvector's default is
# 40; the retriever asks for 30 candidates before re-ranking, so 40 leaves
# little room for the graph's approximation. 100 is measured in
# benchmarks/STEP4_CORPUS.md.
HNSW_EF_SEARCH = 100

# Column list shared by every read, so a row tuple and this list cannot drift.
_COLUMNS = (
    "chunk_id", "content", "token_count", "content_type",
    "company", "ticker", "cik", "accession",
    "filing_type", "fiscal_period", "section", "chunk_index", "source_url",
)


class VectorStoreNotReady(RuntimeError):
    """The filing_chunks table is missing. Run `alembic upgrade head`."""


@asynccontextmanager
async def connection():
    """A connection with a transaction open, for scripts that have no request.

    Inside the API, pass the request's session instead -- anything with an
    awaitable `.execute()` works here.
    """
    async with engine.begin() as conn:
        yield conn


# ─────────────────────────────────────────────────────────────────────────────
# Backend detection
# ─────────────────────────────────────────────────────────────────────────────

async def detect_backend(conn) -> str:
    """Which embedding column type the live schema actually has.

    Read from the catalog rather than remembered in config: the schema is the
    only thing that knows, and a mismatch would produce a SQL error at query
    time instead of at startup.
    """
    row = (await conn.execute(text(
        "SELECT udt_name FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = 'embedding'"
    ), {"t": TABLE})).first()

    if row is None:
        raise VectorStoreNotReady(
            f"table {TABLE!r} not found -- run `alembic upgrade head`"
        )
    if row[0] != "vector":
        # information_schema spells an array of real as '_float4'.
        raise VectorStoreNotReady(
            f"{TABLE}.embedding is {row[0]!r}, not pgvector's 'vector'. Run Postgres "
            f"with pgvector (docs/DATABASE.md) and `alembic upgrade head`; migration "
            f"0005 converts the column."
        )
    return BACKEND_PGVECTOR


def _bind(vector: Sequence[float], backend: str = BACKEND_PGVECTOR):
    """The Python value to bind for a vector parameter: pgvector's text form.

    asyncpg infers each parameter's type from the SQL, not from the value, and
    has no codec for the extension's binary format, so the string goes in as
    text and Postgres converts. That is why `_expr` casts through text rather
    than writing the more obvious `:qvec::vector`, which would make asyncpg try
    to encode a type it does not know.
    """
    return "[" + ",".join(f"{float(v):.7g}" for v in vector) + "]"


def _expr(param: str, backend: str = BACKEND_PGVECTOR) -> str:
    """SQL for a bound vector parameter, cast to pgvector's type."""
    return f"CAST(CAST(:{param} AS text) AS vector)"


# ─────────────────────────────────────────────────────────────────────────────
# Writing
# ─────────────────────────────────────────────────────────────────────────────

async def upsert_chunks(
    conn,
    chunks: Iterable[Any],
    vectors: np.ndarray,
    model: str = EMBEDDING_MODEL,
    backend: str | None = None,
) -> int:
    """Insert or replace chunks with their embeddings, keyed by `chunk_id`.

    Upsert rather than insert so re-indexing a filing is idempotent. A rerun
    after a chunker change should update rows in place; appending a second copy
    of every chunk would corrupt retrieval quietly, by making duplicates compete
    for the top-k.
    """
    chunks = list(chunks)
    if not chunks:
        return 0
    if len(chunks) != len(vectors):
        raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
    if vectors.shape[1] != EMBEDDING_DIM:
        raise ValueError(f"expected {EMBEDDING_DIM}-dim vectors, got {vectors.shape[1]}")

    backend = backend or await detect_backend(conn)

    sql = text(f"""
        INSERT INTO {TABLE} (
            chunk_id, content, token_count, content_type,
            company, ticker, cik, accession,
            filing_type, fiscal_period, period_end, filing_date,
            section, chunk_index, source_url,
            structured, embedding_model, embedding
        ) VALUES (
            :chunk_id, :content, :token_count, :content_type,
            :company, :ticker, :cik, :accession,
            :filing_type, :fiscal_period,
            CAST(NULLIF(:period_end, '') AS date),
            CAST(NULLIF(:filing_date, '') AS date),
            :section, :chunk_index, :source_url,
            CAST(:structured AS jsonb), :embedding_model, {_expr('embedding', backend)}
        )
        ON CONFLICT (chunk_id) DO UPDATE SET
            content          = EXCLUDED.content,
            token_count      = EXCLUDED.token_count,
            content_type     = EXCLUDED.content_type,
            company          = EXCLUDED.company,
            ticker           = EXCLUDED.ticker,
            cik              = EXCLUDED.cik,
            accession        = EXCLUDED.accession,
            filing_type      = EXCLUDED.filing_type,
            fiscal_period    = EXCLUDED.fiscal_period,
            period_end       = EXCLUDED.period_end,
            filing_date      = EXCLUDED.filing_date,
            section          = EXCLUDED.section,
            chunk_index      = EXCLUDED.chunk_index,
            source_url       = EXCLUDED.source_url,
            structured       = EXCLUDED.structured,
            embedding_model  = EXCLUDED.embedding_model,
            embedding        = EXCLUDED.embedding,
            indexed_at       = now()
    """)

    params = []
    for chunk, vector in zip(chunks, vectors):
        meta = chunk.metadata
        params.append({
            "chunk_id": chunk.chunk_id,
            "content": chunk.content,
            "token_count": chunk.token_count,
            "content_type": meta.content_type,
            "company": meta.company,
            "ticker": meta.ticker,
            "cik": meta.cik,
            "accession": meta.accession,
            "filing_type": meta.filing_type,
            "fiscal_period": meta.fiscal_period,
            "period_end": meta.period_end,
            "filing_date": meta.filing_date,
            "section": meta.section,
            "chunk_index": meta.chunk_index,
            "source_url": meta.source_url,
            "structured": json.dumps(chunk.structured) if chunk.structured else None,
            "embedding_model": model,
            "embedding": _bind(vector, backend),
        })

    await conn.execute(sql, params)
    return len(params)


async def delete_accession(conn, accession: str) -> int:
    result = await conn.execute(
        text(f"DELETE FROM {TABLE} WHERE accession = :a"), {"a": accession}
    )
    return result.rowcount or 0


# ─────────────────────────────────────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SearchHit:
    """One retrieved passage, with enough provenance to cite it.

    `score` is the value results are ordered by. When the retriever re-ranks
    (see rag/retriever.py) it is the vector score plus the lexical and
    table-affinity components, and each part is reported separately below so a
    ranking can be explained rather than trusted.
    """

    score: float                # the value ranked on
    chunk_id: str
    content: str
    company: str
    ticker: str
    section: str
    fiscal_period: str
    chunk_index: int
    content_type: str
    filing_type: str
    source_url: str
    token_count: int
    structured: dict | None = None

    # Score breakdown. On a plain vector search `vector_score == score` and the
    # other two are zero.
    vector_score: float = 0.0   # cosine similarity in [-1, 1]; 1 is identical
    lexical_score: float = 0.0  # share of the query's financial terms present
    boost: float = 0.0          # table affinity, when the query wants a figure

    @property
    def citation(self) -> str:
        return (f"{self.ticker} {self.filing_type} {self.fiscal_period} - "
                f"{self.section} #{self.chunk_index}")


def _row_to_hit(row) -> SearchHit:
    mapping = row._mapping
    raw = mapping["structured"]
    return SearchHit(
        score=float(mapping["score"]),
        vector_score=float(mapping["score"]),
        chunk_id=mapping["chunk_id"],
        content=mapping["content"],
        company=mapping["company"],
        ticker=mapping["ticker"],
        section=mapping["section"],
        fiscal_period=mapping["fiscal_period"],
        chunk_index=mapping["chunk_index"],
        content_type=mapping["content_type"],
        filing_type=mapping["filing_type"],
        source_url=mapping["source_url"],
        token_count=mapping["token_count"],
        structured=json.loads(raw) if raw else None,
    )


async def search(
    conn,
    query_vector: Sequence[float],
    k: int = 5,
    *,
    ticker: str | None = None,
    company: str | None = None,
    section: str | None = None,
    content_type: str | None = None,
    fiscal_period: str | None = None,
    min_score: float | None = None,
    backend: str | None = None,
    exact: bool = False,
) -> list[SearchHit]:
    """Cosine-similarity search with optional metadata filtering.

    `exact` skips the HNSW index and scans every row: the true top-k, for
    measuring what the index returns (scripts/retrieval_latency.py). The
    `SET LOCAL` settings last until the caller's transaction ends.

    Filters are SQL predicates evaluated *before* the top-k, not a filter applied
    to the results afterwards. That distinction is the whole point: post-filtering
    a k=5 result set for one company routinely returns nothing.
    """
    backend = backend or await detect_backend(conn)
    qvec = _expr("qvec", backend)
    params: dict[str, Any] = {"qvec": _bind(query_vector, backend), "k": k}

    # <=> is cosine distance; similarity is 1 - distance. Ordering by the raw
    # operator expression is what lets the HNSW index be used.
    score_sql = f"1 - (embedding <=> {qvec})"
    order_sql = f"embedding <=> {qvec} ASC"

    where = ["embedding IS NOT NULL"]
    if ticker:
        where.append("ticker = :ticker")
        params["ticker"] = ticker.strip().upper()
    if company:
        # Forgiving on purpose: callers say "NVDA", "NVIDIA" or "NVIDIA CORP",
        # and a retrieval filter that silently matches nothing is worse than a
        # slightly loose one.
        where.append("(ticker = :company_exact OR company ILIKE :company_like)")
        params["company_exact"] = company.strip().upper()
        params["company_like"] = f"%{company.strip()}%"
    if section:
        where.append("section ILIKE :section")
        params["section"] = f"%{section}%"
    if content_type:
        where.append("content_type = :content_type")
        params["content_type"] = content_type
    if fiscal_period:
        where.append("fiscal_period = :fiscal_period")
        params["fiscal_period"] = fiscal_period

    if not exact:
        # For this transaction only. See the module docstring: without the
        # iterative scan a filtered HNSW search returns fewer than k rows.
        await conn.execute(text(f"SET LOCAL hnsw.ef_search = {int(HNSW_EF_SEARCH)}"))
        await conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    else:
        # The exact answer, for measuring the index against: no index scan.
        await conn.execute(text("SET LOCAL enable_indexscan = off"))

    columns = ", ".join(_COLUMNS)
    sql = f"""
        SELECT {columns}, structured::text AS structured, {score_sql} AS score
        FROM {TABLE}
        WHERE {' AND '.join(where)}
        ORDER BY {order_sql}
        LIMIT :k
    """
    rows = (await conn.execute(text(sql), params)).fetchall()
    hits = [_row_to_hit(row) for row in rows]

    if min_score is not None:
        hits = [h for h in hits if h.score >= min_score]
    return hits


async def stats(conn) -> dict:
    """What is indexed, for a demo script or a health endpoint."""
    backend = await detect_backend(conn)
    rows = (await conn.execute(text(f"""
        SELECT ticker, company, filing_type, fiscal_period,
               COUNT(*) AS chunks,
               COUNT(*) FILTER (WHERE content_type = 'table') AS tables,
               MIN(embedding_model) AS model
        FROM {TABLE}
        GROUP BY 1, 2, 3, 4
        ORDER BY 1
    """))).fetchall()
    total = (await conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}"))).scalar_one()
    return {
        "backend": backend,
        "total_chunks": total,
        "documents": [dict(row._mapping) for row in rows],
    }
