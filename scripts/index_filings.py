"""Fetch, chunk, embed and index filings; report what each stage cost.

    python scripts/index_filings.py                 # NVDA, AAPL: the pinned filings
    python scripts/index_filings.py MSFT --form 10-K            # latest 10-K
    python scripts/index_filings.py AAPL=0000320193-25-000079   # one exact filing
    python scripts/index_filings.py AAPL --latest               # ignore the pin

NVDA and AAPL are pinned by accession number (ingestion/pipeline.py,
PINNED_FILINGS), because the eval questions and the retrieval markers were
written against those two filings. "Latest" moves when a company files again.

Embedding on CPU is the slow stage by a wide margin, so it is timed separately
and reported as measured wall-clock, not an estimate.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ingestion.pipeline import PINNED_FILINGS  # noqa: E402
from rag.embeddings import EMBEDDING_MODEL, MAX_SEQUENCE_TOKENS  # noqa: E402
from rag.vector_store import connection, stats  # noqa: E402

RULE = "=" * 78


def progress_printer(ticker: str):
    def report(done: int, total: int) -> None:
        print(f"\r  embedding {ticker}: {done}/{total} chunks", end="", flush=True)
        if done >= total:
            print()
    return report


def resolve_targets(args: list[str], *, latest: bool = False) -> list[tuple[str, str | None]]:
    """(ticker, accession or None) for each argument.

    `TICKER=ACCESSION` names one filing. A bare ticker gets its pinned filing
    when it has one, unless `latest` is set; any other ticker gets the latest.
    """
    targets = []
    for arg in args:
        ticker, _, accession = arg.partition("=")
        ticker = ticker.strip().upper()
        accession = accession.strip() or None
        if accession is None and not latest:
            accession = PINNED_FILINGS.get(ticker)
        targets.append((ticker, accession))
    return targets


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tickers", nargs="*", default=["NVDA", "AAPL"],
                    help="TICKER, or TICKER=ACCESSION for one exact filing")
    ap.add_argument("--form", default="10-K")
    ap.add_argument("--latest", action="store_true",
                    help="index the latest filing even for a pinned ticker")
    args = ap.parse_args()
    wanted = resolve_targets(args.tickers or ["NVDA", "AAPL"], latest=args.latest)
    tickers = [t for t, _ in wanted]

    print(f"{RULE}\nINDEXING {', '.join(tickers)}   embedding model: {EMBEDDING_MODEL}\n{RULE}")
    for ticker, accession in wanted:
        print(f"  {ticker}: {accession or 'latest ' + args.form}")

    async with connection() as conn:
        results = []
        for ticker, accession in wanted:
            from ingestion.pipeline import index_ticker
            from ingestion.edgar_client import EdgarClient
            with EdgarClient() as client:
                results.append(await index_ticker(
                    conn, ticker, client=client, form=args.form, accession=accession,
                    progress=progress_printer(ticker),
                ))

        for r in results:
            t = r.embed
            print(f"\n{r.ticker}  {r.company}  {r.form} {r.fiscal_period}   {r.accession}")
            print(f"  chunks           {r.chunks}  "
                  f"({r.text_chunks} text, {r.table_chunks} table)")
            print(f"  rows written     {r.rows_written}")
            print(f"  fetch            {r.fetch_seconds:7.2f}s   (cached after first run)")
            print(f"  parse            {r.parse_seconds:7.2f}s")
            print(f"  chunk            {r.chunk_seconds:7.2f}s")
            if t:
                print(f"  EMBED            {t.seconds:7.2f}s   "
                      f"{t.per_item_ms:.0f} ms/chunk   {t.per_second:.2f} chunks/s")
                print(f"    longest input  {t.max_input_tokens} tokens "
                      f"(model limit {MAX_SEQUENCE_TOKENS})")
                print(f"    windowed       {t.windowed} of {t.count} chunks "
                      f"(too long for one pass; embedded in overlapping windows)")
            print(f"  store            {r.store_seconds:7.2f}s")
            print(f"  TOTAL            {r.total_seconds:7.2f}s")

        embedded = sum(r.chunks for r in results)
        embed_time = sum(r.embed_seconds for r in results)
        print(f"\n{RULE}")
        print(f"EMBEDDING TOTAL: {embedded} chunks in {embed_time:.2f}s "
              f"({embed_time / embedded * 1000:.0f} ms/chunk, "
              f"{embedded / embed_time:.2f} chunks/s) on CPU")
        print(RULE)

        info = await stats(conn)
        print(f"\nvector store backend: {info['backend']}   "
              f"total rows: {info['total_chunks']}")
        for doc in info["documents"]:
            print(f"  {doc['ticker']:6} {doc['company'][:28]:30} "
                  f"{doc['filing_type']:6} {doc['fiscal_period']:8} "
                  f"{doc['chunks']:4} chunks ({doc['tables']} table)")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
