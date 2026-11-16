"""Print the last N annual 10-Ks of each company, as accession numbers to pin.

    python scripts/pin_corpus.py                  # the 15 corpus companies, 3 each
    python scripts/pin_corpus.py MSFT KO --count 2

Read-only: one SEC submissions request per company, through EdgarClient (the
User-Agent rule and the rate limiter apply). The output is Python, ready to
paste into ingestion/corpus.py. The pins there are what ingestion uses, so a
company filing again does not change the corpus; run this to move them
deliberately.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.corpus import CORPUS_CIK_OVERRIDES, CORPUS_TICKERS  # noqa: E402
from ingestion.edgar_client import EdgarClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--form", default="10-K")
    args = ap.parse_args()
    tickers = [t.upper() for t in args.tickers] or list(CORPUS_TICKERS)

    with EdgarClient(cik_overrides=CORPUS_CIK_OVERRIDES) as client:
        print("CORPUS_FILINGS: dict[str, tuple[str, ...]] = {")
        for ticker in tickers:
            filings = client.recent_filings(ticker, form=args.form, count=args.count)
            if len(filings) < args.count:
                print(f"    # {ticker}: only {len(filings)} {args.form} on EDGAR")
            notes = ", ".join(f"{f.report_date or f.filing_date} filed {f.filing_date}"
                              for f in filings)
            accessions = "".join(f'"{f.accession}", ' for f in filings).rstrip(" ")
            print(f'    "{ticker}": ({accessions}),')
            print(f"    # {filings[0].company_name if filings else ticker}: periods {notes}")
        print("}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
