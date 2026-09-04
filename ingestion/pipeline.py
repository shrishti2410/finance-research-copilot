"""fetch -> parse -> chunk -> embed -> store, for one ticker.

The stages already existed separately and are all independently testable; this
is the wiring, plus the measurements worth having when a run takes minutes.

Indexing is idempotent. Chunk ids are derived from the accession number, and
`upsert_chunks` keys on them, so re-running after a chunker change updates rows
in place instead of leaving both generations in the index competing for the
top-k.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ingestion.chunker import OVERLAP_TOKENS, TARGET_TOKENS, chunk_filing
from ingestion.edgar_client import EdgarClient
from ingestion.parser import parse_10k
from rag.embeddings import EMBEDDING_MODEL, EmbedTiming, embed_passages_timed
from rag.vector_store import upsert_chunks


@dataclass
class IndexResult:
    """What one document's trip through the pipeline cost and produced."""

    ticker: str
    company: str
    form: str
    accession: str
    fiscal_period: str
    source_url: str

    chunks: int = 0
    text_chunks: int = 0
    table_chunks: int = 0
    rows_written: int = 0

    fetch_seconds: float = 0.0
    parse_seconds: float = 0.0
    chunk_seconds: float = 0.0
    store_seconds: float = 0.0
    embed: EmbedTiming | None = field(default=None)

    @property
    def embed_seconds(self) -> float:
        return self.embed.seconds if self.embed else 0.0

    @property
    def total_seconds(self) -> float:
        return (self.fetch_seconds + self.parse_seconds + self.chunk_seconds
                + self.embed_seconds + self.store_seconds)


async def index_ticker(
    conn,
    ticker: str,
    *,
    client: EdgarClient | None = None,
    form: str = "10-K",
    target_tokens: int = TARGET_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
    model: str = EMBEDDING_MODEL,
    progress=None,
) -> IndexResult:
    """Fetch the latest `form` for `ticker`, chunk it, embed it, store it.

    `conn` is an open async connection -- the caller owns the transaction, so a
    failure part-way through a multi-ticker run does not leave one document
    half-indexed.
    """
    owns_client = client is None
    client = client or EdgarClient()

    try:
        started = time.perf_counter()
        filing = client.latest_filing(ticker, form=form)
        html = client.fetch_document(filing)   # served from data/edgar/ on a rerun
        fetch_seconds = time.perf_counter() - started

        started = time.perf_counter()
        parsed = parse_10k(html)
        parse_seconds = time.perf_counter() - started

        started = time.perf_counter()
        chunks = chunk_filing(
            parsed, filing, target_tokens=target_tokens, overlap_tokens=overlap_tokens
        )
        chunk_seconds = time.perf_counter() - started
    finally:
        if owns_client:
            client.close()

    result = IndexResult(
        ticker=filing.ticker,
        company=filing.company_name,
        form=filing.form,
        accession=filing.accession,
        fiscal_period=chunks[0].metadata.fiscal_period if chunks else "",
        source_url=filing.document_url,
        chunks=len(chunks),
        text_chunks=sum(1 for c in chunks if c.metadata.content_type == "text"),
        table_chunks=sum(1 for c in chunks if c.metadata.content_type == "table"),
        fetch_seconds=fetch_seconds,
        parse_seconds=parse_seconds,
        chunk_seconds=chunk_seconds,
    )
    if not chunks:
        return result

    vectors, timing = embed_passages_timed(
        [c.content for c in chunks], model_id=model, progress=progress
    )
    result.embed = timing

    started = time.perf_counter()
    result.rows_written = await upsert_chunks(conn, chunks, vectors, model=model)
    result.store_seconds = time.perf_counter() - started

    return result


async def index_tickers(conn, tickers: list[str], **kwargs) -> list[IndexResult]:
    """Index several tickers through one EDGAR client, so its rate limiter is shared.

    A per-ticker client would each keep their own clock and could, between them,
    exceed the request rate SEC publishes.
    """
    with EdgarClient() as client:
        return [await index_ticker(conn, t, client=client, **kwargs) for t in tickers]
