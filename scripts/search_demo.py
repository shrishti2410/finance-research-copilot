"""Run the retriever against the indexed filings and print the top hits in full.

    python scripts/search_demo.py                       # the three demo queries
    python scripts/search_demo.py "gross margin" --company NVDA --k 5

Prints each hit's cosine similarity, its provenance, and its content, so the
results can be judged for relevance rather than taken on trust.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from rag.retriever import search_filings  # noqa: E402
from rag.vector_store import connection, detect_backend  # noqa: E402

RULE = "=" * 78

# The three the milestone asks for: an unfiltered semantic query, a query whose
# answer lives in the table chunk rather than in prose, and the first one again
# with a company filter -- so the filter's effect is visible as a difference
# between two otherwise identical runs.
DEMO_QUERIES = [
    ("What are the main competitive risks?", {}),
    ("What was total revenue?", {}),
    ("What are the main competitive risks?", {"company": "NVDA"}),
]


def print_hit(rank: int, hit, preview: int | None) -> None:
    filters = f"  [{hit.content_type}]"
    print(f"\n  #{rank}  score {hit.score:.4f}{filters}  {hit.citation}")
    print(f"      chunk_id  {hit.chunk_id}")
    print(f"      source    {hit.source_url}")
    body = hit.content if preview is None else hit.content[:preview]
    indented = "\n".join("      " + line for line in body.splitlines())
    print(f"      {'-' * 66}")
    print(indented)
    if preview is not None and len(hit.content) > preview:
        print(f"      ... [{len(hit.content) - preview} more chars]")


async def run(query: str, filters: dict, k: int, preview: int | None, conn) -> None:
    label = ", ".join(f"{key}={value!r}" for key, value in filters.items()) or "none"
    print(f"\n{RULE}")
    print(f"QUERY: {query!r}")
    print(f"FILTERS: {label}   k={k}")
    print(RULE)

    started = time.perf_counter()
    hits = await search_filings(query, k=k, conn=conn, **filters)
    elapsed = (time.perf_counter() - started) * 1000

    print(f"{len(hits)} hits in {elapsed:.0f} ms (embed + search)")
    if not hits:
        print("  (nothing matched -- is the index populated?)")
    for rank, hit in enumerate(hits, 1):
        print_hit(rank, hit, preview)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query", nargs="?")
    ap.add_argument("--company")
    ap.add_argument("--section")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--preview", type=int, default=None,
                    help="truncate content to N chars (default: print in full)")
    args = ap.parse_args()

    # Load the encoder before anything is timed. The first embed_query call
    # otherwise carries a few seconds of model load and reports it as query
    # latency, which makes the first query look 300x slower than the rest.
    from rag.embeddings import get_encoder
    load_started = time.perf_counter()
    get_encoder()
    print(f"encoder loaded in {time.perf_counter() - load_started:.2f}s")

    async with connection() as conn:
        print(f"vector store backend: {await detect_backend(conn)}")

        if args.query:
            filters = {key: value for key, value in
                       (("company", args.company), ("section", args.section)) if value}
            await run(args.query, filters, args.k, args.preview, conn)
        else:
            for query, filters in DEMO_QUERIES:
                await run(query, filters, args.k, args.preview, conn)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
