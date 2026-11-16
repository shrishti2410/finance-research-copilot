# Hosted run on the step-3 code: 36/40, against 30/40 for hosted run 1

One full 40-case eval on `openai/gpt-oss-120b` served by Groq, run on 2026-11-16
on merged master `d106dae`: step 3 (SEC XBRL facts), the three guard changes, the
year rule, and the fixes that followed step 3. It is a new run on new code, not a
repeat of hosted run 1 (2026-11-15, commit `1b39944`).

**Result: 36/40 passed, against 30/40 for hosted run 1.**
- Seven cases newly pass and one newly fails.
- Each side is one run. The hosted model's run-to-run noise has not been measured,
  because the repeat run was cancelled. Six cases is larger than the three-case
  swings seen between single Qwen runs, but that is not a measurement of this
  model's noise.

## How it was run

| | this run | hosted run 1 |
|---|---|---|
| commit | `d106dae` (master), run from a separate copy of the tree | `1b39944` |
| command | `python -m eval.run_eval --report benchmarks/results/2026-11-16-groq/eval_run.json` | the same, `.../2026-11-15-groq/eval_run1.json` |
| started / finished (UTC) | 2026-11-16 07:20:34 → 07:42:29 | 2026-11-15 06:05:39 → 06:28:18 |
| machine | Windows 11 laptop, 16 GB. Tools, retrieval and Postgres local; inference on Groq | same |
| standby | blocked; no Kernel-Power standby event during the run | blocked |
| provider, models | Groq, `openai/gpt-oss-120b` for both steps, `AGENT_MAX_TOKENS=2048` | same |
| questions, scorer | unchanged | |

The local database was at migration `0004_public_demo` (from a branch in progress)
while this ran. That migration only adds a column and a table this code does not
read.

## Failures by root cause

Assigned with the rule and order in [FAILURE_ANALYSIS.md](FAILURE_ANALYSIS.md),
from the stored traces.

| root cause | hosted run 1 | this run |
|---|---:|---:|
| wrong tool or wrong arguments | 3 | 0 |
| retrieval miss | 3 | 3 |
| grounding-guard refusal | 2 | 0 |
| extraction or arithmetic error | 2 | 1 |
| timeout | 0 | 0 |
| grader error | 0 | 0 |
| **failed** | **10** | **4** |

**Newly passing (7):**

| case | run 1 cause | what changed |
|---|---|---|
| `aapl-gross-margin-tool` | wrong tool | fiscal-year ratios from facts (step 3) |
| `aapl-net-margin-tool` | wrong tool | same |
| `nvda-vs-aapl-gross-margin-gap` | wrong tool | same |
| `nvda-gross-margin-pct-mdna` | guard refusal | the guard's typographic-hyphen fix |
| `aapl-gross-margin-pct-derived` | guard refusal | stated-operand derivations, and the facts tool |
| `nvda-effective-tax-rate-mdna` | arithmetic | not attributed |
| `nvda-implied-pe-on-fy2026-eps` | arithmetic | not attributed |

The "what changed" column is inferred: it names the change that touches the path
the passing trace took. It was not tested case by case.

**Still failing or newly failing (4):**

| case | cause | detail |
|---|---|---|
| `nvda-datacenter-growth` | retrieval miss | Retrieved the segment table, then quoted a 59% "Data Center computing" sub-line. The 68% sentence was in no returned excerpt, as in run 1. |
| `aapl-buyback-program` | retrieval miss | Three filing searches; "$100 billion" is in no excerpt. The run then ended on a provider error, see below. |
| `aapl-dividend-per-share` | retrieval miss | Four filing searches and a news search; "$0.26" in none. Stopped at the 5-iteration limit. |
| `aapl-services-share-derived` | extraction or arithmetic | **Newly failing.** It had revenue from `get_financial_fact` and the statement with Services net sales, then called `calculate_ratio(gross_margin)` three times and never divided. Run 1 passed it at 26.2%. |

The three retrieval misses are the same three questions, with the same symptom, as
in run 1 and in Qwen run-c: the answer's sentence is in no excerpt the search
returned. Step 3 did not touch retrieval. Whether those sentences are in the index
at all was not checked here.

**A failure mode not seen before: 413 from Groq.** `aapl-buyback-program` ended on
`413 Request Entity Too Large` from the provider at iteration 5, after four searches
with up to ten excerpts each. The account's limit is 8,000 tokens a minute, and a
request larger than that can never be served. That this is the cause is inferred:
the proxy logs the status, not Groq's message body.

## Latency, tokens, cost

| | this run | hosted run 1 |
|---|---:|---:|
| p50 | 26.3 s | 28.7 s |
| p95 | 105.6 s | 76.4 s |
| mean | 32.8 s | 33.8 s |
| p50, rate-limit wait subtracted | 2.9 s | 4.3 s |
| p95, rate-limit wait subtracted | 8.0 s | 8.8 s |
| time spent waiting on Groq's rate limit | 1,143 s, in 36 of 40 cases | |
| prompt / completion tokens | 351,797 / 16,481 | 354,190 / 22,162 |
| cached prompt tokens, as Groq reported them | 188,928 (54% of prompt), in 35 cases | not recorded |
| tool calls / iterations per question | 1.57 / 2.60 | |
| cost per run / per query | $0.0627 / $0.0016 | $0.0664 / $0.0017 |

- **Latency.** The wall-clock figures are mostly Groq's 8,000 tokens-a-minute
  limit, not the model. With the waits subtracted, the median question took 2.9 s.
  The waits were measured by the proxy and are stored per case (`rate_limit_wait_ms`).
- **Cost.** Uses $0.15 / $0.60 per million input / output tokens, Groq's published
  price as read on 2026-11-15 and not re-read today. On the free tier nothing was
  billed.
- **Caching.** This settles the open question from 2026-11-15: Groq does report
  cached prompt tokens for this model, 54% of the prompt in this run.
  - The run sent 368,278 tokens and was not stopped by the 200,000 daily limit.
  - That is consistent with Groq's statement that cached tokens do not count
    toward limits, but it is **inferred**, not measured.

## Reproducing

```bash
# API with the hosted provider. Values from .env; the key is never on the command line.
AGENT_PROVIDER=groq AGENT_MODEL=openai/gpt-oss-120b AGENT_ROUTER_MODEL=openai/gpt-oss-120b \
AGENT_MAX_TOKENS=2048 uvicorn api.main:app --port 8000
python -m eval.run_eval --report benchmarks/results/2026-11-16-groq/eval_run.json
python benchmarks/export_eval_traces.py benchmarks/results/2026-11-16-groq/eval_run.json \
    --since 2026-11-16T07:20:00Z --until 2026-11-16T07:43:00Z \
    --out benchmarks/results/2026-11-16-groq/traces.json
```

Raw output, gitignored: `benchmarks/results/2026-11-16-groq/` (`eval_run.json`,
`eval_run.log`, `traces.json`, `run_env_eval_run.txt`, the API logs).

## Excerpt of the run's own summary

```
  overall: 36/40 = 90.0%
  latency   mean    32.8s   median    26.3s   min    2.3s   max   174.0s
  tokens    prompt    351,797   completion   16,481   total    368,278
```
