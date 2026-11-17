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
import re
from dataclasses import dataclass, replace

from rag.embeddings import EMBEDDING_MODEL, embed_query
from rag.vector_store import SearchHit, connection, search

__all__ = [
    "SearchHit", "search_filings", "search_filings_sync", "resolve_section",
    "query_intent", "QueryIntent",
]

# ─────────────────────────────────────────────────────────────────────────────
# Hybrid re-ranking
# ─────────────────────────────────────────────────────────────────────────────
#
# Vector search alone puts the income statement 5th for "What was total
# revenue?" -- behind three MD&A passages that discuss revenue without ever
# stating it. The cause is not a broken index: a rendered statement is mostly
# digits, and a conversational question embeds close to sentences.
#
# Two things fix it, and they are deliberately separate. Chunking now prepends a
# natural-language caption to every table (see ingestion/chunker.py), which is
# the larger of the two effects. What remains is a ranking problem, handled here.
#
# Re-ranking happens in Python over an over-fetched candidate set rather than in
# SQL. That keeps the vector query pure, so pgvector's HNSW index is still usable
# when this runs against a server that has it -- an ORDER BY over a composite
# score cannot use it.

# Terms that name something a financial statement actually reports. A query
# containing one of these is asking about a line item.
_STATEMENT_TERMS: tuple[str, ...] = (
    "revenue", "sales", "top line", "net income", "earnings", "profit",
    "bottom line", "eps", "per share", "margin", "gross profit",
    "operating income", "cost of revenue", "cost of sales", "cogs",
    "income tax", "tax expense", "shares outstanding", "share count",
    "operating expenses", "r&d", "research and development",
)

# Asking for a number. "What was total revenue" wants the statement.
_VALUE_SEEKING = re.compile(
    r"\bwhat (?:was|were|is|are)\b|\bhow much\b|\bhow many\b|\btotal\b"
    r"|\bfigure\b|\bnumber\b|\bamount\b|\breport(?:ed)?\b", re.I
)

# Asking for an explanation. "Why did gross margin fall" wants the MD&A prose
# that explains it, not the grid that states it -- so these suppress the table
# affinity even though the query is full of statement terms. Without this the
# boost would drag tables above the passages that actually answer the question.
_EXPLANATORY = re.compile(
    r"\bwhy\b|\bexplain\b|\bdiscuss\b|\breason\b|\bdriver\b|\bcause\b"
    r"|\battribut\w*\b|\bsaid\b|\bsay\b|\bcommentary\b|\brisk\b"
    r"|\boutlook\b|\bstrategy\b", re.I
)

# ─────────────────────────────────────────────────────────────────────────────
# The fiscal year a question names
# ─────────────────────────────────────────────────────────────────────────────
#
# With three 10-Ks per company indexed, "NVIDIA's R&D in fiscal 2026" came back
# with the FY2024 and FY2025 statements: three years written in nearly the same
# words, and nothing in the search knew which year was asked about. The
# retrieval eval's hit@5 fell from 100% on one filing per company to 75.9% on
# three (benchmarks/STEP4_CORPUS.md).
#
# So a year the question names as a *fiscal* year ("fiscal 2026", "fiscal year
# 2026", "FY2026") filters the search to the filings that report it: that
# year's 10-K and the next two, because a 10-K's statements carry its own year
# and the two before it. Apple's FY2023 net income is in its FY2024 and FY2025
# 10-Ks. A plain date ("May 2025") is not a fiscal year and sets no filter.
# Where several are named ("from fiscal 2025 to fiscal 2026"), the latest wins:
# its filing reports the earlier one too. If the filter matches nothing, the
# search runs again without it rather than returning nothing.
_FISCAL_YEAR = re.compile(r"\b(?:fiscal(?:\s+year)?\s+|FY\s?)((?:19|20)\d{2})\b", re.I)
FILINGS_REPORTING_A_YEAR = 3


def fiscal_periods_named(query: str) -> list[str] | None:
    """The fiscal-period labels of the filings that report the latest fiscal
    year `query` names, or None when it names none."""
    years = [int(y) for y in _FISCAL_YEAR.findall(query or "")]
    if not years:
        return None
    year = max(years)
    return [f"FY{year + i}" for i in range(FILINGS_REPORTING_A_YEAR)]


# Weights. Small on purpose: they reorder near-ties, they do not overrule the
# encoder. Both were set by measuring the four audit queries, not guessed -- see
# tests/test_retrieval.py, which pins the behaviour they produce.
LEXICAL_WEIGHT = 0.06
TABLE_BOOST = 0.09

# How many candidates to pull before re-ranking. Deep enough that a chunk the
# re-rank would promote is actually in the pool -- the AAPL statement sat at
# rank 13 before captions, so a shallow over-fetch would never have seen it.
OVERFETCH = 6
MIN_CANDIDATES = 30

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


@dataclass(frozen=True)
class QueryIntent:
    """What a query appears to be after, and what that implies for ranking."""

    terms: tuple[str, ...]      # statement terms found in the query
    value_seeking: bool         # asks for a figure
    explanatory: bool           # asks for a reason or commentary

    @property
    def wants_a_figure(self) -> bool:
        """Whether to prefer the statement over the prose that discusses it.

        Needs a statement term *and* a value-seeking phrasing, and must not be
        explanatory. "What was total revenue?" qualifies; "Why did gross margin
        fall?" does not, because the answer to that lives in MD&A -- the grid
        states the number but says nothing about why.
        """
        return bool(self.terms) and self.value_seeking and not self.explanatory


def query_intent(query: str) -> QueryIntent:
    lowered = query.lower()
    return QueryIntent(
        terms=tuple(term for term in _STATEMENT_TERMS if term in lowered),
        value_seeking=bool(_VALUE_SEEKING.search(query)),
        explanatory=bool(_EXPLANATORY.search(query)),
    )


def _lexical_score(content: str, terms: tuple[str, ...]) -> float:
    """Share of the query's statement terms that appear in this passage.

    Cheap and deliberately shallow -- it is a tie-breaker over an already
    semantically-ranked candidate set, not a search engine. Its job is to
    separate a passage that states a figure from one that merely talks around
    it.
    """
    if not terms:
        return 0.0
    lowered = content.lower()
    return sum(1 for term in terms if term in lowered) / len(terms)


def rerank(hits: list[SearchHit], intent: QueryIntent, k: int) -> list[SearchHit]:
    """Re-order vector hits using lexical overlap and table affinity.

    Returns hits carrying their score breakdown, so a ranking can be explained.
    """
    prefer_tables = intent.wants_a_figure
    rescored: list[SearchHit] = []

    for hit in hits:
        lexical = _lexical_score(hit.content, intent.terms)
        boost = TABLE_BOOST if (prefer_tables and hit.content_type == "table") else 0.0
        rescored.append(replace(
            hit,
            score=hit.vector_score + LEXICAL_WEIGHT * lexical + boost,
            lexical_score=lexical,
            boost=boost,
        ))

    rescored.sort(key=lambda h: h.score, reverse=True)
    return rescored[:k]


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
    rerank_results: bool = True,
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
        min_score: drop hits scoring below this. Applied after re-ranking, so
            it compares against the final score. Off by default -- a sensible
            threshold is corpus-dependent and picking one before there is an
            eval set would just be a guess with a number on it.
        rerank_results: apply the lexical and table-affinity re-rank. True by
            default; set False for a pure vector search, which is what the
            comparison in the docstring above was measured against.
        conn: an open connection (the request's session, inside the API). When
            omitted, one is opened and closed around this call.

    Returns:
        Up to `k` SearchHit, best first. Each carries `vector_score`,
        `lexical_score` and `boost` alongside the final `score`.
    """
    if not query or not query.strip():
        raise ValueError("query must not be empty")

    # Runs on the event loop thread: ~15ms of CPU for a single short query on
    # this machine. Worth moving to a thread pool only if that stops being true.
    vector = embed_query(query, model)

    intent = query_intent(query)
    # A period the caller passes wins; otherwise the one the question names.
    # See "The fiscal year a question names" above.
    named = None if fiscal_period else fiscal_periods_named(query)
    # Over-fetch, then re-rank. min_score is applied after re-ranking, so a
    # threshold means what the caller thinks it means -- the final score.
    kwargs = dict(
        k=max(k * OVERFETCH, MIN_CANDIDATES) if rerank_results else k,
        company=company,
        section=resolve_section(section),
        content_type=content_type,
        fiscal_period=fiscal_period or named,
    )

    async def run(conn):
        found = await search(conn, vector, **kwargs)
        if not found and named:
            # The named year is not indexed for this company: search all years.
            found = await search(conn, vector, **{**kwargs, "fiscal_period": None})
        return found

    if conn is not None:
        candidates = await run(conn)
    else:
        async with connection() as own_conn:
            candidates = await run(own_conn)

    hits = rerank(candidates, intent, k) if rerank_results else candidates[:k]
    if min_score is not None:
        hits = [hit for hit in hits if hit.score >= min_score]
    return hits


def search_filings_sync(query: str, company: str | None = None,
                        section: str | None = None, **kwargs) -> list[SearchHit]:
    """Blocking wrapper, for scripts and the REPL. Never call this from async code."""
    return asyncio.run(search_filings(query, company, section, **kwargs))
