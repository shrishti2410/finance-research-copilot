# Hosted run on the calculator branch: 34/40, against 36/40 the day before

One full 40-case eval on `openai/gpt-oss-120b` served by Groq, run on 2026-11-17
on `feat/calculator-tool` at `a62cabe`: the `calculate` tool, the prompt rule
"never do arithmetic yourself", and the guard accepting derived figures only from
the calculator. The comparison is
[HOSTED_RUN_2026-11-16.md](HOSTED_RUN_2026-11-16.md), on master `d106dae`, with
the same provider, model and settings.

**Result: 34/40, against 36/40. Read this as inconclusive, not as a regression.**
- 4 of the 6 failures got no answer from the model: two 413s and two timeouts
  after rate-limit waits. Of the 35 cases that did get an answer, 34 passed.
- Each side is one run, and this model's run-to-run noise has not been measured.

## How it was run

| | this run | 2026-11-16 |
|---|---|---|
| commit | `a62cabe` (`feat/calculator-tool`), a separate copy of the tree, 0 dirty files | `d106dae` (master) |
| command | `python -m eval.run_eval --report benchmarks/results/2026-11-17-calculator/eval_run.json` | the same, `.../2026-11-16-groq/eval_run.json` |
| started / finished (UTC) | 2026-11-17 09:30:31 → 10:03:36 | 2026-11-16 07:20:34 → 07:42:29 |
| machine | Windows 11 laptop, 16 GB. Tools, retrieval and Postgres local; inference on Groq | same |
| standby | blocked. Between 09:25 and 10:10 UTC the System log has only Kernel-Power 105 (power source change) events, none for standby | blocked |
| provider, models | Groq, `openai/gpt-oss-120b` for both steps, `AGENT_MAX_TOKENS=2048` | same |
| questions, scorer | unchanged | |

## Failures by root cause

Assigned with the rule and order in [FAILURE_ANALYSIS.md](FAILURE_ANALYSIS.md),
from the stored traces and `eval_run.json`.

| root cause | 2026-11-16 | this run |
|---|---:|---:|
| wrong tool or wrong arguments | 0 | 1 |
| retrieval miss | 3 | 1 |
| grounding-guard refusal | 0 | 0 |
| extraction or arithmetic error | 1 | 0 |
| timeout (inference error) | 0 | 2 |
| provider 413 (inference error) | 0 | 2 |
| grader error | 0 | 0 |
| **failed** | **4** | **6** |

| case | this run | detail |
|---|---|---|
| `nvda-datacenter-growth` | retrieval miss | Quoted the 59% "Data Center computing" sub-line instead of 68%, as on both earlier runs. |
| `aapl-buyback-program` | 413 | Three filing searches, then the provider rejected the fourth request. On 2026-11-16 it was a retrieval miss that also ended on a 413. |
| `aapl-dividend-per-share` | 413 | The same pattern. On 2026-11-16 it was a retrieval miss that hit the 5-iteration limit. |
| `aapl-implied-pe-on-fy2025-eps` | timeout | `ReadTimeout` after 302 s. |
| `nvda-vs-aapl-gross-margin-gap` | timeout | `ReadTimeout` after 301 s, with no tool call. Passed on 2026-11-16. |
| `aapl-debt-to-equity-tool` | wrong tool (no tool) | **Newly failing.** Answered with no tool call that there is no FY2026 Apple filing, so the ratio cannot be computed. `aapl-net-margin-tool` in the same run got FY2026 Apple data from `calculate_ratio`. Passed on 2026-11-16. |
| `aapl-services-share-derived` | **now passes** | It failed on 2026-11-16 because the model never divided. This time it called `calculate`. |

- **Answers refused because the model did its own arithmetic: 0.** No case stopped
  on a refusal for a figure the model computed itself.
- `calculate` was called in 10 of the 40 cases, and all 10 passed.

## Guard false positive, new in this run

Four passing answers carry the `[Unverified]` banner (stop reason
`unverified_figures`): `nvda-diluted-shares-fy2026`, `aapl-revenue-fy2025`,
`aapl-services-fy2025` and `aapl-net-income-fy2023`.
- In each, the model wrote citation markers such as `【0†value】`, and the guard
  read the `0` as a figure no tool returned. The figures themselves were correct
  and graded as passes.
- On 2026-11-16 no answer contained these markers. Three of its cases had the same
  stop reason, for other reasons.
- The score is unchanged, but a user sees a false warning. The fix is a guard
  change and needs approval. It is the first item in the next set of guard
  changes.

## Does the calculator make requests larger? Yes, by about 510 tokens each

| | 2026-11-16 | this run |
|---|---:|---:|
| prompt tokens per question, mean (all 40) | 8,795 | 10,223 |
| prompt tokens per question, median | 6,140 | 7,028 |
| prompt tokens per model call, mean (cases without an inference error) | 3,363 | 3,736 |
| completion tokens per question, mean | 412 | 387 |
| iterations / tool calls per question | 2.60 / 1.57 | 2.80 / 1.68 |
| prompt / completion tokens, whole run | 351,797 / 16,481 | 408,922 / 15,487 |
| cached prompt tokens, as Groq reported them | 188,928 | 233,216 |

- **Measured:** prompt tokens per model call rose 11%, and per question 16%.
  These are the provider's own counts, stored per case.
- **Fixed overhead per request, counted locally:**
  - the `calculate` tool schema is 393 tokens out of 2,350 for all the tool schemas;
  - the new prompt rule is 119 tokens.

  These were counted with `tiktoken`'s `o200k_base` on the `a62cabe` code. That
  approximates Groq's tokenizer but is not the same thing.
- **Inferred:** the rest of the per-question rise is the extra `calculate` step,
  with 2.80 iterations against 2.60. This was not measured case by case.

## The two 413s: request sizes

Groq does not report the size of a rejected request, and the proxy logs only the
status, so these figures are **estimates**. Each is reconstructed from the trace:

- the system prompt: 1,286 tokens;
- the tool schemas: 2,350 tokens;
- three `search_filings` results as compacted for the model;
- plus about 200 tokens for the question and the model's three tool-call turns.

All counts use `o200k_base`.

| case | compacted search results (tokens) | estimated fourth request |
|---|---|---:|
| `aapl-buyback-program` | 986 + 1,726 + 1,687 = 4,399 | about 8,250 |
| `aapl-dividend-per-share` | 1,026 + 1,550 + 1,688 = 4,264 | about 8,100 |

- Both are just above the account's 8,000 tokens-a-minute limit, and a request
  larger than that limit can never be served. The 2026-11-16 record inferred the
  same cause for its 413.
- **Inferred:** without the calculator's roughly 510 tokens, both would be
  about 7,600–7,750 and would probably have been sent. On a tier with the
  8,000-token limit, the calculator therefore lowers the number of
  search-heavy turns that fit.
  - Neither question was answerable from its excerpts on 2026-11-16 (retrieval
    misses), so the 413 probably cost no passes here.
  - The live deploy runs on the same kind of account, so it hits the same
    limit.

## Latency

| | 2026-11-16 | this run |
|---|---:|---:|
| p50 / p95 / mean | 26.3 / 105.6 / 32.8 s | 34.0 / 104.3 / 49.5 s |
| p50 / p95, rate-limit wait subtracted | 2.9 / 8.0 s | 3.8 / 52.1 s |
| time spent waiting on Groq's rate limit | 1,143 s, in 36 cases | 1,188 s, in 33 cases |

The p95 with waits subtracted is dominated by the two 300 s timeouts, which
recorded no proxy wait.

## Reproducing

```bash
# API with the hosted provider. Values from .env; the key is never on the command line.
AGENT_PROVIDER=groq AGENT_MODEL=openai/gpt-oss-120b AGENT_ROUTER_MODEL=openai/gpt-oss-120b \
AGENT_MAX_TOKENS=2048 uvicorn api.main:app --port 8000
python -m eval.run_eval --report benchmarks/results/2026-11-17-calculator/eval_run.json
python benchmarks/export_eval_traces.py benchmarks/results/2026-11-17-calculator/eval_run.json \
    --since 2026-11-17T09:30:00Z --until 2026-11-17T10:04:00Z \
    --out benchmarks/results/2026-11-17-calculator/traces.json
```

Raw output, gitignored: `benchmarks/results/2026-11-17-calculator/` (`eval_run.json`,
`eval_run.log`, `traces.json`, `run_env_eval_run.txt`, the API logs).

## Excerpt of the run's own summary

```
  overall: 34/40 = 85.0%
  latency   mean    49.5s   median    34.0s   min    2.4s   max   302.2s
  tokens    prompt    408,922   completion   15,487   total    424,409
```
