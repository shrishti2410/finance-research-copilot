"""Fetch and parse the latest 10-K for a few tickers, and report what came out.

    python scripts/ingest_demo.py                 # NVDA, AAPL
    python scripts/ingest_demo.py MSFT GOOGL
    python scripts/ingest_demo.py --preview 1000

Prints, per section: character count, a preview so the content can be eyeballed
for boilerplate/TOC contamination, and for the income statement whether it
parsed into usable numbers or fell apart.

Nothing is chunked or embedded here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Filings are full of typographic quotes and dashes; a cp1252 console would
# mangle them and make clean extraction look broken.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ingestion.edgar_client import EdgarClient  # noqa: E402
from ingestion.parser import parse_10k  # noqa: E402

RULE = "=" * 78


def show_section(section, preview_chars: int) -> None:
    if section is None:
        print("  MISSING -- heading not found in the document")
        return
    print(f"  heading      {section.heading!r}")
    print(f"  char count   {section.char_count:,}")
    print(f"  blocks       {section.block_count:,}")
    print(f"  first {preview_chars} characters:")
    print("  " + "-" * 74)
    for line in section.preview(preview_chars).splitlines():
        print(f"  | {line}")
    print("  " + "-" * 74)


def show_income_statement(table, preview_rows: int = 14) -> None:
    if table is None:
        print("  MISSING -- no table in Item 8 matched an income statement")
        return

    total_cells = sum(len(r.values) for r in table.rows)
    labelled = sum(1 for r in table.rows if r.label)
    numeric_rows = sum(1 for r in table.rows if r.values)

    print(f"  title            {table.title!r}")
    print(f"  units            {table.units!r}")
    print(f"  scales           amount x{table.scales.amount:,}  "
          f"share_count x{table.scales.share_count:,}  per_share x{table.scales.per_share}")
    print(f"  match score      {table.match_score} income-statement terms matched")
    print(f"  periods          {table.periods}")
    print(f"  rows             {table.row_count}  ({labelled} labelled, {numeric_rows} with numbers)")
    print(f"  numeric cells    {total_cells}")
    print(f"  first {preview_rows} rows as parsed:")
    print("  " + "-" * 74)
    for row in table.rows[:preview_rows]:
        label = (row.label[:44] or "(no label)").ljust(44)
        values = "  ".join(f"{v:>14,.0f}" if v is not None else f"{'-':>14}" for v in row.values)
        print(f"  | {label} {values}")
    print("  " + "-" * 74)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tickers", nargs="*", default=["NVDA", "AAPL"])
    ap.add_argument("--preview", type=int, default=500, help="preview characters per section")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    tickers = args.tickers or ["NVDA", "AAPL"]

    with EdgarClient() as client:
        for ticker in tickers:
            filing = client.latest_filing(ticker, form="10-K")
            html = client.fetch_document(filing, use_cache=not args.no_cache)
            parsed = parse_10k(html)

            print(f"\n{RULE}\n{ticker}  {filing.form}  filed {filing.filing_date}  "
                  f"period ending {filing.report_date}\n{RULE}")
            print(f"source     {filing.document_url}")
            print(f"raw HTML   {len(html):,} chars")
            print(f"items      {', '.join(parsed.items_found)}")

            print(f"\n-- Item 1A: Risk Factors {'-' * 45}")
            show_section(parsed.risk_factors, args.preview)

            print(f"\n-- Item 7: MD&A {'-' * 54}")
            show_section(parsed.mdna, args.preview)

            print(f"\n-- Income statement (from Item 8) {'-' * 36}")
            show_income_statement(parsed.income_statement)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
