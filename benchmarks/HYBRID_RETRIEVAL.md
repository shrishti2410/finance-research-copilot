# Hybrid retrieval and the named-company filter: the retrieval check

Backlog item 1. Retrieval only: no model, no agent, no Groq. The LLM dev
eval was not run.

**In short:**
- **Hybrid on its own lowers hit@5**, from 86.2% to 75.9%. It fixes the Apple
  dividend question but loses six questions answered by a statement table.
- **Filtering to the one company a question names raises hit@5 to 100%**,
  fixing all three of the named misses.
- **With that filter, hybrid lifts hit@3 from 89.7% to 100%.**
- **The check has saturated.** 29 questions about two companies, each naming
  its company, can no longer separate the two best configurations, and that
  is exactly the case the company filter serves. Both settings stay off by
  default until the held-out set (backlog item 2) can tell.

## How it was run

| | |
|---|---|
| command | `RETRIEVAL_HYBRID={0,1} RETRIEVAL_INFER_COMPANY={0,1} python -m eval.retrieval_eval --report ...` |
| commit | `63e0852` on `feat/hybrid-retrieval` (off `feat/frontend-redesign`, with `fix/retrieval-fiscal-year` merged in) |
| date | 2026-11-17, 06:47-06:48 UTC |
| machine | ASUS Vivobook X1504ZA, Intel Core i5-1235U, Windows 11 |
| data | local pgvector database: 45 10-Ks of 15 companies, 2,914 chunks, migrated to `0009_filing_chunks_fts`; bge-small for the query embedding |
| check | `eval/retrieval_eval.py`, 29 questions, k = 5, re-rank on, no company filter passed by the caller; unchanged |
| raw output | `benchmarks/results/2026-11-17-hybrid/` (gitignored) |

The baseline before the fiscal-year filter comes from the same command on
`feat/frontend-redesign` (`e4f5266`), run at 06:39 UTC.

## What was compared

- **Vector + re-rank:** what the branch does today. A pgvector cosine search
  with the fiscal-year filter, then the lexical and table re-rank.
- **Hybrid** (`RETRIEVAL_HYBRID`):
  - Postgres full-text search runs over a generated `tsvector` column
    (`english`), with a GIN index.
  - The question's words are joined by OR and ranked by `ts_rank_cd` with
    length normalisation, the closest built-in to BM25.
  - It applies the same metadata filters as the vector search.
  - It is fused with the vector + re-rank ranking by Reciprocal Rank Fusion:
    k = 60, 30 candidates a side.
  - Both constants were fixed before the first run and not tuned.
- **Named company** (`RETRIEVAL_INFER_COMPANY`):
  - When a question names exactly one indexed company and the caller passes
    none, both searches are filtered to it.
  - A filter that finds nothing falls back to all companies.
  - This variant was declared after the hybrid result and before its own
    run. It is the metadata-filter part of the item's spec.

## Results (measured)

| configuration | hit@5 | hit@3 | recall@5 | recall@3 | misses at 5 |
|---|---|---|---|---|---|
| redesign branch, no fiscal-year filter | 22/29 (75.9%) | 21/29 (72.4%) | 0.557 | | 7 |
| vector + re-rank (fiscal-year filter) | 25/29 (86.2%) | 22/29 (75.9%) | 0.712 | 0.510 | 4 |
| + hybrid | 22/29 (75.9%) | 19/29 (65.5%) | 0.539 | 0.453 | 7 |
| + named company | **29/29 (100%)** | 26/29 (89.7%) | **0.865** | 0.711 | 0 |
| + named company + hybrid | **29/29 (100%)** | **29/29 (100%)** | 0.833 | **0.735** | 0 |

The baseline's recall@5 of 0.557 is from `benchmarks/FISCAL_YEAR_FILTER.md`,
measured on the same check.

**The three named misses** (rank of the first gold passage; "-" means not
in the top 5):

| configuration | Data Center growth | Apple buyback | Apple dividend |
|---|---|---|---|
| no fiscal-year filter | miss | miss | miss |
| vector + re-rank | hit (4) | miss | miss |
| + hybrid | hit (2) | miss | hit (3) |
| + named company | hit (4) | hit (3) | hit (4) |
| + named company + hybrid | hit (2) | hit (1) | hit (3) |

## Why (inferred from the per-question output)

- **Hybrid alone, the losses.**
  - Most of the six questions it breaks are answered by a statement table:
    NVIDIA's gross profit, Apple's Services revenue and share, the market cap
    and P/E derivations.
  - Full-text search ranks the prose that repeats the question's words, and
    RRF cancels the re-rank's preference for the table.
  - Across 15 companies, words like "gross profit" or "revenue" match every
    company's 10-K, so the full-text list is diluted.
- **The company filter.** It removes exactly that dilution: the Apple buyback
  question was retrieving JPMorgan's and NVIDIA's repurchase passages.
- **Together.** Within one company's filings the full-text ranking adds
  information. The words "repurchase" and "dividend" pick out the capital
  return passage, which lifts it to rank 1 to 3.

## Not measured

- The end-to-end dev eval with either setting (needs the hosted model).
- Questions that do not name their company, such as follow-ups. The company
  filter cannot help those, and the hybrid-alone result suggests hybrid could
  hurt them.
- Latency of the extra full-text query.
- Neon: migration 0009 adds the column and index there at deploy.
