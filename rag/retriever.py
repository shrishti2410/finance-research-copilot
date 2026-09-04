"""Query -> ranked filing passages.

The one function anything downstream should need:

    hits = await search_filings("What are the main competitive risks?",
                                company="NVDA", section="risk")

It embeds the query with the BGE retrieval prefix, then runs a cosine search in
Postgres with the metadata filters pushed into the WHERE clause.

Filters are pushed down, not applied afterwards
-----------------------------------------------
`company` and `section` become SQL predicates evaluated *before* the top-k, so
"the 3 best NVDA passages" really is that. The alternative -- retrieve k, then
drop the rows that do not match -- routinely returns nothing, because with two
companies indexed a global top-3 is often all one company's.

Filter values are forgiving on purpose. `company` accepts a ticker or any
substring of the registered name ("NVDA", "NVIDIA", "NVIDIA CORP"), and
`section` accepts the shorthand a caller would actually type ("risk", "mdna",
"1A"). A retrieval filter that silently matches nothing is a worse failure than
one that matches slightly too much: the empty result looks like "the corpus does
not cover this" rather than "you spelled the section wrong".

Not here yet: query rewriting, re-ranking, and multi-query fusion. Those are
worth adding once there is an eval set to prove they help; added now they would
be untested complexity between the query and the answer.
"""

from __future__ import annotations

import asyncio

from rag.embeddings import EMBEDDING_MODEL, embed_query
from rag.vector_store import SearchHit, connection, search

__all__ = ["SearchHit", "search_filings", "search_filings_sync", "resolve_section"]

# Shorthand -> the substring matched against the stored `section` value.
# Stored values are "Item 1A - Risk Factors", "Item 7 - Management's
# Discussion...", and "Income Statement".
_SECTION_ALIASES = {
    "1a": "Item 1A",
    "item 1a": "Item 1A",
    "risk": "Item 1A",
    "risks": "Item 1A",
    "risk factors": "Item 1A",
    "7": "Item 7",
    "item 7": "Item 7",
    "mdna": "Item 7",
    "md&a": "Item 7",
    "mda": "Item 7",
    "discussion": "Item 7",
    "management's discussion": "Item 7",
    "income": "Income Statement",
    "income statement": "Income Statement",
    "financials": "Income Statement",
    "table": "Income Statement",
}


def resolve_section(section: str | None) -> str | None:
    """Map caller shorthand onto the stored section string.

    An unrecognised value is passed through rather than rejected, so
    `section="Supplementary"` still works as a literal substring match once
    other sections are indexed.
    """
    if not section:
        return None
    return _SECTION_ALIASES.get(section.strip().lower(), section.strip())


async def search_filings(
    query: str,
    company: str | None = None,
    section: str | None = None,
    *,
    k: int = 5,
    content_type: str | None = None,
    fiscal_period: str | None = None,
    min_score: float | None = None,
    conn=None,
    model: str = EMBEDDING_MODEL,
) -> list[SearchHit]:
    """Cosine-similarity search over indexed filings, newest ranking first.

    Args:
        query: natural-language question.
        company: ticker or a substring of the company name. None searches all.
        section: "risk" / "mdna" / "income", an "Item 1A"-style string, or None.
        k: how many passages to return.
        content_type: "text" or "table", to force or exclude the statements.
        min_score: drop hits below this cosine similarity. Off by default --
            a sensible threshold is corpus-dependent and picking one before
            there is an eval set would just be a guess with a number on it.
        conn: an open connection (the request's session, inside the API). When
            omitted, one is opened and closed around this call.

    Returns:
        Up to `k` SearchHit, highest cosine similarity first.
    """
    if not query or not query.strip():
        raise ValueError("query must not be empty")

    # Runs on the event loop thread: ~15ms of CPU for a single short query on
    # this machine. Worth moving to a thread pool only if that stops being true.
    vector = embed_query(query, model)

    kwargs = dict(
        k=k,
        company=company,
        section=resolve_section(section),
        content_type=content_type,
        fiscal_period=fiscal_period,
        min_score=min_score,
    )

    if conn is not None:
        return await search(conn, vector, **kwargs)

    async with connection() as own_conn:
        return await search(own_conn, vector, **kwargs)


def search_filings_sync(query: str, company: str | None = None,
                        section: str | None = None, **kwargs) -> list[SearchHit]:
    """Blocking wrapper, for scripts and the REPL. Never call this from async code."""
    return asyncio.run(search_filings(query, company, section, **kwargs))
