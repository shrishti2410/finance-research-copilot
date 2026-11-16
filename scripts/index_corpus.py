"""Index the step-4 corpus: 15 companies, three pinned 10-Ks each, and their facts.

    python scripts/index_corpus.py                    # everything not yet indexed
    python scripts/index_corpus.py MSFT KO            # some companies only
    python scripts/index_corpus.py --force            # re-index what is there too
    python scripts/index_corpus.py --facts-only       # XBRL facts only
    python scripts/index_corpus.py --report out.json  # also write the measurements

Resumable and idempotent
------------------------
Each filing is one transaction: its old rows are deleted and its new rows
written together, then committed. So a filing is either fully indexed or not
at all, and an interrupted run (a laptop that sleeps, a 503 from SEC, Ctrl-C)
loses at most the filing in flight. A rerun skips every filing whose accession
already has rows, unless --force, and carries on with the rest. A filing that
fails is reported and the run continues; the exit code says whether any did.

Facts are the same per company: delete and write in one transaction. They are
cheap (one request each, cached on disk), so they are re-written on every run.

SEC rules
---------
One EdgarClient for the whole run, so one rate limiter governs every request
(SEC_REQUESTS_PER_SECOND, under SEC's stated 10 per second) and the client
refuses to start without a User-Agent that names a contact. Filing documents
and company-facts JSON are cached under EDGAR_CACHE_DIR, so a rerun fetches
nothing it already has.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import delete, text  # noqa: E402

from db.base import engine  # noqa: E402
from db.models import XbrlFact  # noqa: E402
from ingestion.corpus import (  # noqa: E402
    CORPUS_CIK_OVERRIDES,
    CORPUS_FACTS_THROUGH,
    CORPUS_FILINGS,
    CORPUS_TICKERS,
)
from ingestion.edgar_client import EdgarClient  # noqa: E402
from ingestion.pipeline import PINNED_FACTS_THROUGH, PINNED_FILINGS, index_ticker  # noqa: E402
from ingestion.xbrl_facts import annual_facts  # noqa: E402
from rag.vector_store import delete_accession  # noqa: E402
from tools.facts import upsert_facts  # noqa: E402


async def already_indexed(accession: str) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT COUNT(*) FROM filing_chunks WHERE accession = :a"), {"a": accession}
        )).scalar_one()


async def index_one(client: EdgarClient, ticker: str, accession: str) -> dict:
    """One filing, in one transaction. Returns its measurements."""
    started = time.perf_counter()
    async with engine.begin() as conn:
        # Delete first, in the same transaction: a re-index that produces fewer
        # chunks than last time must not leave the surplus behind.
        replaced = await delete_accession(conn, accession)
        result = await index_ticker(conn, ticker, client=client, accession=accession)
    return {
        "ticker": ticker, "accession": accession, "status": "indexed",
        "company": result.company, "fiscal_period": result.fiscal_period,
        "chunks": result.chunks, "text_chunks": result.text_chunks,
        "table_chunks": result.table_chunks, "replaced_rows": replaced,
        "fetch_s": round(result.fetch_seconds, 2), "parse_s": round(result.parse_seconds, 2),
        "chunk_s": round(result.chunk_seconds, 2), "embed_s": round(result.embed_seconds, 2),
        "store_s": round(result.store_seconds, 2),
        "total_s": round(time.perf_counter() - started, 2),
    }


async def index_facts(client: EdgarClient, ticker: str) -> dict:
    started = time.perf_counter()
    payload = client.company_facts(ticker)
    # The eval companies keep the eval's pin; the rest take the corpus's.
    through = PINNED_FACTS_THROUGH if ticker in PINNED_FILINGS else CORPUS_FACTS_THROUGH
    rows = annual_facts(payload, ticker, filed_through=through)
    async with engine.begin() as conn:
        await conn.execute(delete(XbrlFact).where(XbrlFact.ticker == ticker))
        written = await upsert_facts(conn, rows)
    years = sorted({r.fiscal_year for r in rows})
    return {"ticker": ticker, "facts": written, "through": through.isoformat(),
            "fiscal_years": f"{years[0]}-{years[-1]}" if years else "",
            "seconds": round(time.perf_counter() - started, 2)}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--force", action="store_true", help="re-index filings already present")
    ap.add_argument("--facts-only", action="store_true")
    ap.add_argument("--no-facts", action="store_true")
    ap.add_argument("--report", type=Path, help="write the measurements as JSON here")
    args = ap.parse_args()
    tickers = [t.upper() for t in args.tickers] or list(CORPUS_TICKERS)
    unknown = [t for t in tickers if t not in CORPUS_FILINGS]
    if unknown:
        print(f"not in the corpus: {', '.join(unknown)}", file=sys.stderr)
        return 2

    run_started = time.perf_counter()
    filings, facts, failures = [], [], 0
    with EdgarClient(cik_overrides=CORPUS_CIK_OVERRIDES) as client:
        if not args.facts_only:
            for ticker in tickers:
                for accession in CORPUS_FILINGS[ticker]:
                    present = await already_indexed(accession)
                    if present and not args.force:
                        filings.append({"ticker": ticker, "accession": accession,
                                        "status": "skipped", "chunks": present})
                        print(f"  {ticker:5} {accession}  already indexed ({present} chunks)")
                        continue
                    try:
                        row = await index_one(client, ticker, accession)
                    except Exception as exc:  # noqa: BLE001 - reported, and the run goes on
                        failures += 1
                        row = {"ticker": ticker, "accession": accession, "status": "failed",
                               "error": f"{type(exc).__name__}: {exc}"[:300]}
                        print(f"  {ticker:5} {accession}  FAILED {row['error']}")
                    else:
                        print(f"  {ticker:5} {accession}  {row['fiscal_period']:8} "
                              f"{row['chunks']:4} chunks  embed {row['embed_s']:6.1f}s  "
                              f"total {row['total_s']:6.1f}s")
                    filings.append(row)
        if not args.no_facts:
            for ticker in tickers:
                try:
                    row = await index_facts(client, ticker)
                    print(f"  {ticker:5} facts {row['facts']:5}  years {row['fiscal_years']}  "
                          f"through {row['through']}")
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    row = {"ticker": ticker, "status": "failed",
                           "error": f"{type(exc).__name__}: {exc}"[:300]}
                    print(f"  {ticker:5} facts FAILED {row['error']}")
                facts.append(row)

    async with engine.connect() as conn:
        totals = (await conn.execute(text(
            "SELECT COUNT(*), COUNT(DISTINCT accession), COUNT(DISTINCT ticker) "
            "FROM filing_chunks"))).one()
    await engine.dispose()
    report = {
        "seconds": round(time.perf_counter() - run_started, 1),
        "indexed_now": sum(1 for f in filings if f.get("status") == "indexed"),
        "skipped": sum(1 for f in filings if f.get("status") == "skipped"),
        "failed": failures,
        "table_totals": {"chunks": totals[0], "filings": totals[1], "companies": totals[2]},
        "filings": filings, "facts": facts,
    }
    print(f"\nrun {report['seconds']}s: {report['indexed_now']} indexed, {report['skipped']} "
          f"skipped, {failures} failed.  filing_chunks now: {totals[0]} chunks, "
          f"{totals[1]} filings, {totals[2]} companies")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
