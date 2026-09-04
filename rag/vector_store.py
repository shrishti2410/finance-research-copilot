"""Chunk + embedding storage, in the same Postgres the rest of the app uses.

One table, `filing_chunks`, holding the chunk text, its metadata, and its
384-dimensional embedding. No separate vector database: at this corpus size a
second service would be pure operational cost, and keeping vectors next to the
relational data means a metadata filter is a WHERE clause rather than a
post-filter over an approximate result set.

Two storage backends, chosen by the migration, detected here at runtime
=====================================================================
`vector(384)` from **pgvector** is the target and what production runs. It gives
a cosine-distance operator and an HNSW index.

Where the pgvector extension is not installed, migration 0002 falls back to a
plain `real[]` column and this module computes the dot product in SQL. That is a
sequential scan with no ANN index -- fine for the ~113 chunks in this repo,
useless at a million.

**The two backends return identical scores.** `rag/embeddings.py` emits
L2-normalized vectors, so cosine similarity equals the inner product, and both
expressions below compute exactly that. The fallback changes how fast a search
is, not what it returns -- which is what makes numbers measured on the fallback
worth reporting.
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
BACKEND_ARRAY = "float_array"

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
    # information_schema spells an array of real as '_float4'.
    return BACKEND_PGVECTOR if row[0] == "vector" else BACKEND_ARRAY


def _bind(vector: Sequence[float], backend: str):
    """The Python value to bind for a vector parameter.

    asyncpg infers each parameter's type from the SQL, not from the value, so
    the value has to match what the cast in `_expr` asks for:

      * `real[]` -- a list of floats, which asyncpg encodes as a Postgres array.
      * `vector` -- pgvector's own text form, bound as text. asyncpg has no codec
        for the extension's binary format, so the string goes in as text and
        Postgres converts. That is why `_expr` casts through text rather than
        writing the more obvious `:qvec::vector`, which would make asyncpg try to
        encode a type it does not know.
    """
    if backend == BACKEND_PGVECTOR:
        return "[" + ",".join(f"{float(v):.7g}" for v in vector) + "]"
    return [float(v) for v in vector]


def _expr(param: str, backend: str) -> str:
    """SQL for a bound vector parameter, cast to the column's type."""
    if backend == BACKEND_PGVECTOR:
        return f"CAST(CAST(:{param} AS text) AS vector)"
    return f"CAST(:{param} AS real[])"


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
    """One retrieved passage, with enough provenance to cite it."""

    score: float                # cosine similarity in [-1, 1]; 1 is identical
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

    @property
    def citation(self) -> str:
        return (f"{self.ticker} {self.filing_type} {self.fiscal_period} - "
                f"{self.section} #{self.chunk_index}")


def _row_to_hit(row) -> SearchHit:
    mapping = row._mapping
    raw = mapping["structured"]
    return SearchHit(
        score=float(mapping["score"]),
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
) -> list[SearchHit]:
    """Cosine-similarity search with optional metadata filtering.

    Filters are SQL predicates evaluated *before* the top-k, not a filter applied
    to the results afterwards. That distinction is the whole point: post-filtering
    a k=5 result set for one company routinely returns nothing.
    """
    backend = backend or await detect_backend(conn)
    qvec = _expr("qvec", backend)
    params: dict[str, Any] = {"qvec": _bind(query_vector, backend), "k": k}

    if backend == BACKEND_PGVECTOR:
        # <=> is cosine distance; similarity is 1 - distance. Ordering by the
        # raw operator expression is what lets the HNSW index be used.
        score_sql = f"1 - (embedding <=> {qvec})"
        order_sql = f"embedding <=> {qvec} ASC"
    else:
        # Both operands are unit vectors, so the dot product *is* the cosine.
        score_sql = (f"(SELECT COALESCE(SUM(a::float8 * b::float8), 0) "
                     f"FROM unnest(embedding, {qvec}) AS t(a, b))")
        order_sql = "score DESC"

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
