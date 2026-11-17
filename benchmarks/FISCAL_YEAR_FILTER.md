# Retrieval: filtering on the fiscal year a question names

**Problem.** On the 15-company corpus (three 10-Ks per company), the retrieval
eval's hit@5 fell to 75.9%, against 100% with one filing per company. Questions
about one fiscal year retrieved another year's filing.

**Change.** When a question names a fiscal year ("fiscal 2026", "fiscal year
2024", "FY2023"), `search_filings` filters to the filings that report that year:
that year's 10-K and the next two, since a 10-K's statements carry its own year
and the two before it.
- Several years named: the latest one is used.
- A calendar date ("May 2025") sets no filter.
- A filter that matches nothing falls back to all years.

Code: `rag/retriever.py`, `fiscal_periods_named`.

**Result: hit@5 75.9% to 86.2%. Answers in a table: 91.3% to 100.0%.**

| retrieval eval, 29 questions, no company filter, k=5 | before | after |
|---|---:|---:|
| hit@3 | 72.4% | 75.9% |
| **hit@5** | **75.9%** | **86.2%** |
| recall@5 | 0.557 | 0.712 |
| answer in a table: hit@5 (23) | 91.3% | **100.0%** |
| answer in prose: hit@5 (6) | 16.7% | 33.3% |
| NVDA: hit@5 (16) | 68.8% | 87.5% |
| AAPL: hit@5 (13) | 84.6% | 84.6% |

The eval is deterministic (no model), so one run each is the measurement.

## How it was run

| | |
|---|---|
| command | `python -m eval.retrieval_eval --report before.json`, then the same with the change (`after.json`) |
| code | before: `feat/step4-corpus` at `4b169f8`; after: the same tree plus this change |
| corpus | pgvector container on port 55433: 45 filings, 15 companies, 2,914 chunks (`benchmarks/STEP4_CORPUS.md`) |
| date, machine | 2026-11-16, Windows 11 laptop, CPU embeddings, no model called |
| raw output | `benchmarks/results/2026-11-16-fiscal-year/` (`before.txt`, `after.txt`, and the JSON) |

## Still missed after the change

| question | why |
|---|---|
| `aapl-buyback-program`, `aapl-dividend-per-share` | they name "May 2025", a calendar date, so no filter applies; with one Apple filing indexed these were hits |
| `nvda-effective-tax-rate-mdna`, `nvda-gross-margin-pct-mdna` | answers in NVIDIA's MD&A prose; the eval applies no company filter, so other companies' FY2026 MD&As compete |

The agent usually passes a company to `search_filings`, which this eval
deliberately does not. So these four are the hard case, not the common one.
