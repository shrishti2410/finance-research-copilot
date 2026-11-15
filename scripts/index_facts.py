"""Fetch SEC XBRL company facts and store the annual figures in Postgres.

    python scripts/index_facts.py                 # NVDA, AAPL: facts as of the pinned date
    python scripts/index_facts.py MSFT            # everything SEC has
    python scripts/index_facts.py NVDA --latest   # ignore the pin
    python scripts/index_facts.py NVDA --refresh  # fetch again, ignoring the disk cache

NVDA and AAPL are pinned (ingestion/pipeline.py): their facts are taken as
filed through PINNED_FACTS_THROUGH, for the same reason their filings are
pinned by accession number. SEC's document grows with each new report.

One request per company to data.sec.gov/api/xbrl/companyfacts, through the same
EdgarClient as the filings: the SEC User-Agent rule and the rate limiter apply.
The JSON is cached under EDGAR_CACHE_DIR. Indexing is idempotent: a company's
rows are deleted and written again in one transaction, so nothing is duplicated
and nothing from an earlier, wider run is left behind.

Run `alembic upgrade head` first; the table is created by migration 0003.
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

from sqlalchemy import delete  # noqa: E402

from db.base import engine  # noqa: E402
from db.models import XbrlFact  # noqa: E402
from ingestion.edgar_client import EdgarClient  # noqa: E402
from ingestion.pipeline import PINNED_FACTS_THROUGH, PINNED_FILINGS  # noqa: E402
from ingestion.xbrl_facts import annual_facts  # noqa: E402
from tools.facts import upsert_facts  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tickers", nargs="*", default=["NVDA", "AAPL"])
    ap.add_argument("--refresh", action="store_true",
                    help="fetch from SEC again instead of using the cached JSON")
    ap.add_argument("--latest", action="store_true",
                    help="take everything filed to date, even for a pinned company")
    args = ap.parse_args()
    tickers = [t.upper() for t in (args.tickers or ["NVDA", "AAPL"])]

    # One client for the run, so its rate limiter is shared across companies.
    with EdgarClient() as client:
        for ticker in tickers:
            started = time.perf_counter()
            payload = client.company_facts(ticker, use_cache=not args.refresh)
            fetched = time.perf_counter() - started
            through = (PINNED_FACTS_THROUGH
                       if ticker in PINNED_FILINGS and not args.latest else None)
            rows = annual_facts(payload, ticker, filed_through=through)
            scope = f"as filed through {through.isoformat()}" if through else "all filings to date"
            async with engine.begin() as conn:
                # Replace, not merge: a row the pin now excludes must not survive
                # from a run made without it.
                await conn.execute(delete(XbrlFact).where(XbrlFact.ticker == ticker))
                written = await upsert_facts(conn, rows)
            years = sorted({r.fiscal_year for r in rows})
            print(f"{ticker}  {payload.get('entityName', '')}   ({scope})")
            print(f"  facts written    {written}  "
                  f"({len({r.concept for r in rows})} concepts)")
            if years:
                print(f"  fiscal years     {years[0]}-{years[-1]}")
            print(f"  fetch            {fetched:7.2f}s   (cached after first run)")
            print(f"  total            {time.perf_counter() - started:7.2f}s")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
