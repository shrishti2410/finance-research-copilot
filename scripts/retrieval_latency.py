"""Retrieval latency over the indexed corpus, and what the HNSW index returns
against an exact scan.

    python scripts/retrieval_latency.py --report out.json

For each query: the end-to-end `search_filings` call the agent makes (embed,
HNSW search for the re-rank candidates, re-rank), then, for the same query
vector, the SQL search alone through the index and as an exact scan, and the
overlap of the two top-k sets. Queries are filtered to one company, as the
agent's are, plus a set with no filter.

Read-only. The first call is a warm-up (model load, connection) and is not
counted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402

from db.base import engine  # noqa: E402
from ingestion.corpus import CORPUS_TICKERS  # noqa: E402
from rag.embeddings import embed_query  # noqa: E402
from rag.retriever import MIN_CANDIDATES, search_filings  # noqa: E402
from rag.vector_store import HNSW_EF_SEARCH, connection, search  # noqa: E402

TOPICS = (
    "main risks to the business",
    "revenue growth and what drove it",
    "competition in its markets",
)
UNFILTERED = (
    "export controls on semiconductors", "interest rate risk on the loan book",
    "streaming subscriber growth", "oil and gas production volumes",
    "electric vehicle deliveries", "cloud computing revenue",
    "payment network transaction volume", "pharmaceutical litigation",
    "share repurchase program", "supply chain concentration",
)


def pct(values, p):
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    low, high = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def summary(values):
    return {"n": len(values), "p50_ms": round(pct(values, 0.5), 1),
            "p95_ms": round(pct(values, 0.95), 1), "mean_ms": round(statistics.mean(values), 1),
            "max_ms": round(max(values), 1)}


async def one(query: str, company: str | None, k_values=(5, MIN_CANDIDATES)) -> dict:
    started = time.perf_counter()
    await search_filings(query, company)
    end_to_end = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    vector = embed_query(query)
    embed_ms = (time.perf_counter() - started) * 1000

    row = {"query": query, "company": company, "end_to_end_ms": end_to_end,
           "embed_ms": embed_ms}
    for k in k_values:
        async with connection() as conn:
            started = time.perf_counter()
            approx = await search(conn, vector, k=k, company=company)
            row[f"hnsw_k{k}_ms"] = (time.perf_counter() - started) * 1000
        async with connection() as conn:
            started = time.perf_counter()
            exact = await search(conn, vector, k=k, company=company, exact=True)
            row[f"exact_k{k}_ms"] = (time.perf_counter() - started) * 1000
        want = {h.chunk_id for h in exact}
        row[f"recall_at_{k}"] = len(want & {h.chunk_id for h in approx}) / max(len(want), 1)
        row[f"returned_k{k}"] = len(approx)
    return row


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()

    async with engine.connect() as conn:
        corpus = (await conn.execute(text(
            "SELECT COUNT(*), COUNT(DISTINCT accession), COUNT(DISTINCT ticker) "
            "FROM filing_chunks"))).one()
        plan = "\n".join(r[0] for r in (await conn.execute(text(
            "EXPLAIN SELECT chunk_id FROM filing_chunks ORDER BY embedding <=> "
            "(SELECT embedding FROM filing_chunks LIMIT 1) LIMIT 30"))).all())
    print(f"corpus: {corpus[0]} chunks, {corpus[1]} filings, {corpus[2]} companies; "
          f"hnsw.ef_search {HNSW_EF_SEARCH}")
    print("unfiltered plan uses the HNSW index:", "ix_filing_chunks_embedding_hnsw" in plan)

    await one("warm-up query", None)                      # not counted
    rows = []
    for ticker in CORPUS_TICKERS:
        for topic in TOPICS:
            rows.append(await one(topic, ticker))
    for query in UNFILTERED:
        rows.append(await one(query, None))
    await engine.dispose()

    filtered = [r for r in rows if r["company"]]
    unfiltered = [r for r in rows if not r["company"]]
    report = {
        "corpus": {"chunks": corpus[0], "filings": corpus[1], "companies": corpus[2]},
        "hnsw_ef_search": HNSW_EF_SEARCH, "hnsw_in_unfiltered_plan":
            "ix_filing_chunks_embedding_hnsw" in plan,
        "queries": len(rows),
    }
    for label, group in (("filtered", filtered), ("unfiltered", unfiltered), ("all", rows)):
        report[label] = {
            "end_to_end": summary([r["end_to_end_ms"] for r in group]),
            "embed": summary([r["embed_ms"] for r in group]),
            f"hnsw_k{MIN_CANDIDATES}": summary([r[f"hnsw_k{MIN_CANDIDATES}_ms"] for r in group]),
            f"exact_k{MIN_CANDIDATES}": summary([r[f"exact_k{MIN_CANDIDATES}_ms"] for r in group]),
            "recall_at_5_mean": round(statistics.mean(r["recall_at_5"] for r in group), 4),
            f"recall_at_{MIN_CANDIDATES}_mean": round(
                statistics.mean(r[f"recall_at_{MIN_CANDIDATES}"] for r in group), 4),
            "short_results": sum(1 for r in group if r[f"returned_k{MIN_CANDIDATES}"] < MIN_CANDIDATES),
        }
    report["rows"] = rows
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
