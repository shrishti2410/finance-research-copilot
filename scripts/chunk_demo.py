"""Chunk the latest 10-K for a few tickers and print the result for inspection.

    python scripts/chunk_demo.py                    # NVDA, AAPL
    python scripts/chunk_demo.py --examples 3
    python scripts/chunk_demo.py MSFT --target 400 --overlap 40

Prints per document: total chunk count, a token-count distribution, a few
example chunks in full (never truncated -- the point is to check they do not end
mid-sentence), and the structured table chunk in full.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ingestion.chunker import TARGET_TOKENS, OVERLAP_TOKENS, chunk_filing  # noqa: E402
from ingestion.edgar_client import EdgarClient  # noqa: E402
from ingestion.parser import parse_10k  # noqa: E402

RULE = "=" * 78


def print_chunk(chunk, label: str) -> None:
    meta = chunk.metadata
    print(f"\n{'─' * 78}")
    print(f"{label}   chunk_id={chunk.chunk_id}")
    print(f"{'─' * 78}")
    print("metadata:")
    print(f"  company        {meta.company!r}")
    print(f"  filing_type    {meta.filing_type!r}")
    print(f"  fiscal_period  {meta.fiscal_period!r}")
    print(f"  section        {meta.section!r}")
    print(f"  chunk_index    {meta.chunk_index}")
    print(f"  content_type   {meta.content_type!r}   ticker={meta.ticker}  "
          f"period_end={meta.period_end}")
    print(f"tokens: {chunk.token_count}   hard_split: {chunk.hard_split}")
    print(f"{'─' * 78}")
    print(chunk.content)
    print(f"{'─' * 78}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tickers", nargs="*", default=["NVDA", "AAPL"])
    ap.add_argument("--target", type=int, default=TARGET_TOKENS)
    ap.add_argument("--overlap", type=int, default=OVERLAP_TOKENS)
    ap.add_argument("--examples", type=int, default=3)
    args = ap.parse_args()
    tickers = args.tickers or ["NVDA", "AAPL"]

    with EdgarClient() as client:
        for ticker in tickers:
            filing = client.latest_filing(ticker, form="10-K")
            parsed = parse_10k(client.fetch_document(filing))
            chunks = chunk_filing(
                parsed, filing, target_tokens=args.target, overlap_tokens=args.overlap
            )

            text_chunks = [c for c in chunks if c.metadata.content_type == "text"]
            table_chunks = [c for c in chunks if c.metadata.content_type == "table"]
            counts = [c.token_count for c in text_chunks]

            print(f"\n{RULE}\n{ticker} - {filing.company_name}  {filing.form}  "
                  f"period ending {filing.report_date}\n{RULE}")
            print(f"TOTAL CHUNKS: {len(chunks)}"
                  f"   ({len(text_chunks)} text, {len(table_chunks)} table)")

            by_section: dict[str, int] = {}
            for c in chunks:
                by_section[c.metadata.section] = by_section.get(c.metadata.section, 0) + 1
            for section, n in by_section.items():
                print(f"    {n:3}  {section}")

            if counts:
                print(f"  token counts: min={min(counts)} median={int(statistics.median(counts))} "
                      f"max={max(counts)} mean={int(statistics.mean(counts))}  "
                      f"(target {args.target}, overlap {args.overlap})")
            over = [c for c in text_chunks if c.token_count > args.target]
            hard = [c for c in text_chunks if c.hard_split]
            print(f"  chunks over target: {len(over)}   hard-split (cut mid-sentence): {len(hard)}")

            # Spread the examples across the document so they cover both
            # sections rather than three consecutive chunks from the top.
            if text_chunks and args.examples:
                step = max(1, len(text_chunks) // args.examples)
                picks = [text_chunks[min(i * step, len(text_chunks) - 1)]
                         for i in range(args.examples)]
                seen = set()
                for chunk in picks:
                    if chunk.chunk_id in seen:
                        continue
                    seen.add(chunk.chunk_id)
                    print_chunk(chunk, f"EXAMPLE TEXT CHUNK  [{ticker}]")

            for chunk in table_chunks:
                print_chunk(chunk, f"STRUCTURED TABLE CHUNK  [{ticker}]")
                print("structured payload (as stored alongside the text):")
                print(f"  title   {chunk.structured['title']!r}")
                print(f"  units   {chunk.structured['units']!r}")
                print(f"  scales  {chunk.structured['scales']}")
                print(f"  periods {chunk.structured['periods']}")
                print(f"  rows    {len(chunk.structured['rows'])}")
                for row in chunk.structured["rows"]:
                    print(f"    {row['label'][:46]:46} {row['values']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
