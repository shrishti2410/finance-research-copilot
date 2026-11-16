# Step 4 corpus: 15 companies, 45 10-Ks, pgvector with HNSW

**What was built.**
- **Corpus.** The last three annual 10-Ks of 15 companies, pinned by accession
  number, and their XBRL facts.
- **Storage.** Indexed into Postgres with pgvector and an HNSW index; the plain
  `real[]` fallback is gone from the search path.
- **Ingestion** is resumable and idempotent.

**Results.**

| | measured |
|---|---|
| corpus | 45 filings, 15 companies, **2,914 chunks**, 62,387 facts rows |
| ingestion from an empty database | **1,165 s** (19.4 min) wall, including one deliberate interruption; 84% of it embedding on the laptop CPU |
| interruption and resume | killed after 241 s with 10 filings committed; the rerun skipped those 10 and indexed 35; the result is chunk-for-chunk identical to an uninterrupted index |
| `search_filings` end to end | **p50 66 ms, p95 88 ms**, 55 queries; about half of it is embedding the query |
| HNSW against an exact scan | the same top 5 and top 30 as the exact scan for all 55 queries, at the same speed (p50 26 ms against 25 ms): the corpus is too small for the index to matter yet |
| retrieval eval, hit@5 | **75.9%**, against 100.0% on the two eval filings alone: other years of the same company crowd out the year asked about |

One run each, on 2026-11-16. Each figure is from the commands below, on the code
named.

## How it was run

| | |
|---|---|
| code | `feat/step4-corpus` at `ad9b768` (clean tree); the retrieval-eval baseline on master `8942e61` |
| date | 2026-11-16, 11:31 to 12:29 UTC |
| machine | Windows 11 laptop, 16 GB, 12 logical CPUs; embeddings on CPU (bge-small, torch); standby blocked for every run |
| database | `pgvector/pgvector:pg16` in Docker (the image CI uses), pgvector 0.8.7, on port 55433. Not the native Postgres on 55432, which the calculator eval scheduled for 2026-11-17 reads |
| SEC | EdgarClient, one client per run: User-Agent with a contact, `SEC_REQUESTS_PER_SECOND` (5) under SEC's 10. Documents cached under `data/edgar/` after the first fetch |
| model | none: no LLM was called, so no hosted tokens were spent |

Commands (raw output in `benchmarks/results/2026-11-16-step4-corpus/`, gitignored):

```bash
python scripts/pin_corpus.py                      # resolve the 45 accession numbers (read-only)
python scripts/index_corpus.py --report ingest.json
python scripts/index_corpus.py MSFT GOOGL AMZN META JPM WMT NFLX --force --no-facts  # after the parser fix
python scripts/retrieval_latency.py --report retrieval_latency.json
python -m eval.retrieval_eval --report retrieval_eval_corpus.json   # step-4 code, pgvector corpus
python -m eval.retrieval_eval --report retrieval_eval_baseline.json # master, the two eval filings
# from an empty database, interrupted after 240 s, then resumed:
python scripts/index_corpus.py --report fresh_part1.json   # killed
python scripts/index_corpus.py --report fresh_part2.json
```

## The corpus

| company | filings | chunks | Risk Factors | MD&A | statement tables | facts rows |
|---|---:|---:|---:|---:|---:|---:|
| AAPL | 3 | 131 | 98 | 30 | 3 | 3,920 |
| AMZN | 3 | 160 | 82 | 75 | 3 | 4,196 |
| GOOGL | 3 | 196 | 109 | 84 | 3 | 3,013 |
| JNJ | 3 | 183 | 59 | 124 | 0 | 4,742 |
| JPM | 3 | 161 | 158 | 3 | 0 | 6,888 |
| KO | 3 | 302 | 128 | 171 | 3 | 5,355 |
| META | 3 | 365 | 261 | 101 | 3 | 2,935 |
| MSFT | 3 | 181 | 103 | 75 | 3 | 4,388 |
| NFLX | 3 | 164 | 99 | 62 | 3 | 3,194 |
| NVDA | 3 | 212 | 151 | 58 | 3 | 4,231 |
| PG | 3 | 192 | 50 | 142 | 0 | 3,913 |
| TSLA | 3 | 194 | 108 | 83 | 3 | 3,842 |
| V | 3 | 193 | 107 | 83 | 3 | 4,561 |
| WMT | 3 | 231 | 124 | 104 | 3 | 3,593 |
| XOM | 3 | 49 | 46 | 3 | 0 | 3,616 |
| **total** | **45** | **2,914** | | | **33** | **62,387** |

Database: 57 MB in all; `filing_chunks` 17 MB, of which the HNSW index is 5.5 MB.

Pins (`ingestion/corpus.py`) were resolved from SEC on 2026-11-16. Facts are
taken as filed through 2026-11-16; NVDA and AAPL keep the eval's 2026-11-15 pin.

Two things in SEC's data needed handling:
- **Older 10-Ks are on paginated pages.** SEC's `filings.recent` list holds a
  company's last 1,000 filings, which for JPMorgan contained one 10-K and for
  Meta two. The client now reads the older pages.
- **"XOM" has moved.** Since July 2026 SEC maps "XOM" to ExxonMobil Holdings
  Corp (CIK 2115436), which has filed no 10-K yet. Exxon Mobil Corp's 10-Ks are
  under CIK 34088, which the corpus pins.

### Parser coverage

The first full run found no Risk Factors and no MD&A in 18 of 45 filings: the
parser had been written against NVIDIA's and Apple's markup. Three variations were
fixed (commit `ad9b768`, with tests), and the 20 affected filings re-indexed.

| | before the fix | after |
|---|---:|---:|
| filings with a Risk Factors section | 27 of 45 | 45 of 45 |
| filings with an MD&A section | 25 of 45 | 45 of 45 |
| of which more than a cross-reference (over 1,000 characters) | 22 | 39 |
| filings with an income-statement table | 33 of 45 | 33 of 45 |
| chunks | 1,984 | 2,914 |
| what the parser extracts from the two eval filings | | byte-identical |

Still missing, and why. In each case the content is not where the parser looks:
- **JPMorgan's MD&A** (3 chunks). Its 10-K says the MD&A "appears on pages
  46–160" of the annual report, which is a separate exhibit.
- **Exxon Mobil's MD&A** (3 chunks). Item 7 says "Reference is made to ... the
  Financial Section of this report", a part of the document with no Item heading.
- **No income-statement table** for JNJ, JPM, PG and XOM. The facts tool still
  has their figures (62,387 rows across all 15 companies).

## Ingestion time

From an empty database, fresh process, documents already in the local cache:

| run | what it did | wall time |
|---|---|---:|
| part 1 | 10 filings committed, then killed mid-filing at 241 s | 241 s |
| part 2 | skipped the 10, indexed the other 35 filings, then facts for 15 companies | 923.6 s |
| **total** | 45 filings and all facts | **1,164.6 s** |

Part 2 per filing:
- **Time per filing:** median 22.9 s, ranging from 5.6 s (Exxon, 14 chunks) to
  45.7 s (Meta, 126 chunks).
- **Embedding:** 84.0% of filing time, at 318 ms a chunk on the laptop CPU.
- **Facts:** 43.1 s for all 15 companies.

Fetching from SEC is not in these figures, because the documents were already
cached. On the first run, which did fetch, `fetch_s` per filing was 0.07 to 4.8 s
(`ingest.json`, `reindex.json`).

## Resumable and idempotent: checked, not assumed

- **Interrupted at 241 s by killing the process.** The database then held 10
  complete filings (589 chunks) and nothing of the filing in flight: each filing
  is one transaction.
- **Resumed.** "35 indexed, 10 skipped, 0 failed."
- **Same result.** The fresh database and the main one, indexed in a different
  order and with a forced re-index of seven companies, are identical. An MD5 over
  every `chunk_id` and its content's MD5 is `5ee9f3da...` in both, with 2,914 rows
  each, and both hold 62,387 facts rows.

## Retrieval latency and the HNSW index

`scripts/retrieval_latency.py`. 45 queries filtered to one company (three topics
for each of the 15) and 10 unfiltered. One warm-up query is not counted.
`hnsw.ef_search = 100`, `hnsw.iterative_scan = relaxed_order`.

| | filtered (n=45) | unfiltered (n=10) |
|---|---|---|
| `search_filings` end to end | p50 65.9 ms, p95 88.2 ms | p50 67.3 ms, p95 79.1 ms |
| of which: embedding the query | p50 34.3 ms | p50 34.3 ms |
| SQL, k=30, through HNSW | p50 26.1 ms, p95 46.6 ms | p50 26.6 ms, p95 33.0 ms |
| SQL, k=30, exact scan | p50 25.5 ms, p95 45.2 ms | p50 24.4 ms, p95 27.5 ms |
| HNSW recall against exact, top 5 / top 30 | 1.00 / 1.00 | 1.00 / 1.00 |
| searches returning fewer than 30 rows | 0 | 0 |

- **The plan uses the index:** `EXPLAIN` of an unfiltered search shows
  `ix_filing_chunks_embedding_hnsw`.
- **At 2,914 rows the index buys nothing yet.** An exact scan is as fast. It
  will matter at a corpus some orders of magnitude larger; this run does not
  measure where.
- **The SQL time is mostly fetching rows and the round trip,** not the
  distance computation.

## Retrieval quality: a bigger corpus costs hit rate

`eval/retrieval_eval.py`: the 29 eval questions with a gold passage, no company
filter, k=5, re-ranking on.

| | hit@3 | hit@5 | recall@5 |
|---|---:|---:|---:|
| 2 eval filings (master, native Postgres, exact scan) | 86.2% | 100.0% | 0.989 |
| 45 filings (step 4, pgvector HNSW) | 72.4% | 75.9% | 0.557 |
| answer in a table, 45 filings | 91.3% | 91.3% | |
| answer in prose, 45 filings | 0.0% | 16.7% | |

**What goes wrong.** The questions name a fiscal year; the corpus now holds three
of each company's years, written in near-identical language.
- "NVIDIA's R&D in fiscal 2026" retrieves the FY2024 and FY2025 statements.
- Apple's buyback question retrieves Walmart and NVIDIA passages.

The HNSW index is not the cause: it returned the exact top-k for every query
measured above. The ranker is the cause. It has no notion of the year a question
asks about, and this eval applies no company filter, which the agent usually does.

**Not fixed here.** Candidate fixes, for a decision:
- pass the fiscal year the question names as a `fiscal_period` filter, or as a
  re-rank boost;
- index one year per company for the eval.

This does not affect the 40-question eval, which runs on the eval's own two
pinned filings.

## Not measured

- Retrieval at a corpus large enough for the index to beat an exact scan.
- The facts tool's latency over 15 companies.
- Anything end to end with a model: no agent eval was run on this corpus.
- First-fetch time from SEC for all 45 documents in one run. They were fetched
  across the first run and the pin script, and their `fetch_s` is recorded per
  filing.
