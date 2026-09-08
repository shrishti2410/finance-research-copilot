"""Which chunks ought to come back for each filing-answerable question.

Gold sets are defined by **evidence markers** -- a literal string that must
appear in a chunk for that chunk to contain the answer -- and resolved against
the corpus at run time.

Why markers rather than chunk ids in a file
-------------------------------------------
Two reasons, and the second is the important one.

A chunk id is `accession:section:index`, so it moves whenever the chunker's
sizing changes. A gold file full of ids silently rots into a file full of ids
that match nothing, and a retrieval score of zero looks like a broken retriever
rather than a stale fixture.

More importantly: a marker can be checked. "The chunk that answers 'what was
NVIDIA's revenue' is the one containing the string 215,938" is a claim anyone
can verify against the corpus. "The gold chunk is
0001045810-26-000021:IS:0" is a claim you have to take on faith -- and the
easiest way to produce that file is to run the retriever and write down what it
returned, which measures nothing at all.

A marker that resolves to no chunk, or to suspiciously many, is a bug in the
gold set and `resolve_gold` reports it rather than scoring around it.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["MARKERS", "GoldSet", "resolve_gold"]


# case id -> (ticker, literal string that identifies a chunk holding the answer)
#
# Table cases use the figure as it is rendered in the statement, which is how a
# person would find it. MD&A cases use a distinctive fragment of the sentence
# that states the fact.
MARKERS: dict[str, tuple[str, str]] = {
    # ── NVDA income statement ───────────────────────────────────────────────
    "nvda-revenue-fy2026": ("NVDA", "215,938"),
    "nvda-gross-profit-fy2026": ("NVDA", "153,463"),
    "nvda-rnd-fy2026": ("NVDA", "18,497"),
    "nvda-net-income-fy2026": ("NVDA", "120,067"),
    "nvda-revenue-fy2024": ("NVDA", "60,922"),
    "nvda-diluted-eps-fy2026": ("NVDA", "4.90"),
    "nvda-diluted-shares-fy2026": ("NVDA", "24,514"),
    "nvda-effective-tax-rate-derived": ("NVDA", "141,450"),
    "nvda-revenue-growth-derived": ("NVDA", "130,497"),
    "nvda-operating-margin-derived": ("NVDA", "130,387"),
    "nvda-implied-market-cap": ("NVDA", "24,514"),
    "nvda-implied-pe-on-fy2026-eps": ("NVDA", "4.90"),

    # ── AAPL income statement ───────────────────────────────────────────────
    "aapl-revenue-fy2025": ("AAPL", "416,161"),
    "aapl-services-fy2025": ("AAPL", "109,158"),
    "aapl-gross-margin-dollars-fy2025": ("AAPL", "195,201"),
    "aapl-net-income-fy2025": ("AAPL", "112,010"),
    "aapl-net-income-fy2023": ("AAPL", "96,995"),
    "aapl-basic-eps-fy2025": ("AAPL", "7.49"),
    "aapl-basic-shares-fy2025": ("AAPL", "14,948,500"),
    "aapl-gross-margin-pct-derived": ("AAPL", "195,201"),
    "aapl-services-share-derived": ("AAPL", "109,158"),
    "aapl-implied-market-cap": ("AAPL", "15,004,697"),
    "aapl-implied-pe-on-fy2025-eps": ("AAPL", "7.46"),

    # ── MD&A prose ──────────────────────────────────────────────────────────
    "nvda-gross-margin-pct-mdna": ("NVDA", "Gross margins decreased to 71.1%"),
    "nvda-effective-tax-rate-mdna": ("NVDA", "Income tax as a percentage of income"),
    "nvda-buyback-authorization": ("NVDA", "repurchase up to $58.5 billion"),
    "nvda-datacenter-growth": ("NVDA", "Data Center revenue for fiscal year 2026"),
    "aapl-buyback-program": ("AAPL", "share repurchase program of up to $100 billion"),
    "aapl-dividend-per-share": ("AAPL", "raised its quarterly dividend"),
}


@dataclass(frozen=True)
class GoldSet:
    """The chunks that contain the answer to one question."""

    case_id: str
    marker: str
    ticker: str
    chunk_ids: frozenset[str]

    @property
    def ok(self) -> bool:
        return bool(self.chunk_ids)


async def resolve_gold(session, markers: dict[str, tuple[str, str]] = MARKERS
                       ) -> dict[str, GoldSet]:
    """Turn markers into chunk ids by scanning the corpus.

    A marker is matched against the chunk's own ticker as well as its text, so
    "4.90" cannot resolve to an Apple chunk that happens to contain it.
    """
    from sqlalchemy import text

    rows = (await session.execute(text(
        "SELECT chunk_id, ticker, content FROM filing_chunks"
    ))).mappings().all()

    resolved: dict[str, GoldSet] = {}
    for case_id, (ticker, marker) in markers.items():
        hits = frozenset(
            row["chunk_id"] for row in rows
            if row["ticker"] == ticker and marker in row["content"]
        )
        resolved[case_id] = GoldSet(case_id, marker, ticker, hits)
    return resolved
