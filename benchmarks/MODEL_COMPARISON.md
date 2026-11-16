# Hosted model against the Qwen baseline: DRAFT, one of two hosted runs done

> **Superseded for the hosted model by [HOSTED_RUN_2026-11-16.md](HOSTED_RUN_2026-11-16.md)**: 36/40 on the step-3 code (`d106dae`), against 30/40 for hosted run 1 below. The hosted noise repeat was cancelled, so the empty "hosted run 2" column stays empty. This file is kept as the record of hosted run 1 against the Qwen baseline.

**Status: draft.** The first hosted run is in. The second was started straight
after it and stopped at case 12 of 40, because the provider's daily token
allowance ran out ([why](#why-there-is-one-hosted-run-so-far)). Its results
are not used. The run-to-run noise of the hosted model is therefore **not
measured yet**, and every "hosted run 2" cell below is empty on purpose.

**Result so far.** Hosted run 1 scored **30/40 = 75.0%** on
`openai/gpt-oss-120b` served by Groq. The baseline, run-c, scored
**27/40 = 67.5%** on `qwen2.5:7b` with a `qwen2.5:1.5b` router on local
Ollama. These are two different systems: a different model, a different
provider, one model for both steps instead of two, and a larger output cap.
Three cases is also inside what one eval run moves by chance here, so this is
**not yet a measured improvement**. Eight cases passed only on the hosted
model and five only on Qwen.

Every number comes from the commands in [Reproducing](#reproducing). Raw
outputs are in `benchmarks/results/2026-11-15-groq/` and
`benchmarks/results/2026-11-11-eval/` (both gitignored). The failure
categories and the rule for assigning them are the ones in
[FAILURE_ANALYSIS.md](FAILURE_ANALYSIS.md).

## How each side was run

| | hosted run 1 | Qwen baseline (run-c) |
|---|---|---|
| commit | `1b39944` (`feat/pluggable-provider`) | `d3125b4` (master) |
| command | `python -m eval.run_eval --report benchmarks/results/2026-11-15-groq/eval_run1.json` | `python -m eval.run_eval --report benchmarks/results/2026-11-11-eval/eval_run.json` |
| started / finished (UTC) | 2026-11-15 06:05:39 → 06:28:18 (23 min) | 2026-11-11 11:19:32 → 12:43:04 (84 min) |
| machine | Windows 11 laptop, 16 GB, CPU only. Tools and retrieval local, inference remote | same laptop, inference local |
| standby | blocked for the whole run | blocked for the whole run |
| provider | Groq, `https://api.groq.com/openai/v1`, through the app's proxy | Ollama 0.40.16 |
| answer model | `openai/gpt-oss-120b` | `qwen2.5:7b` |
| router model | the same model, so no router split | `qwen2.5:1.5b` |
| `AGENT_MAX_TOKENS` | 2048 (a reasoning model spends part of the cap on reasoning) | 512 |
| iterations / tool calls per iteration | 5 / 8 | 5 / 8 |
| index, questions, scorer, guard | unchanged from run-c | |

The agent code between the two commits differs only by the provider setting
and the rate-limit wait in the proxy. No question, scorer or guard file
changed.

## Accuracy

| | hosted run 1 | hosted run 2 | Qwen baseline |
|---|---:|---:|---:|
| passed | 30/40 = 75.0% | not run | 27/40 = 67.5% |
| wrong (a number outside tolerance) | 5 | | 5 |
| no number | 5 | | 8 |
| request errors | 0 | | 0 |

By what the question can be answered with:

| answerable via | n | hosted run 1 | hosted run 2 | Qwen baseline |
|---|---:|---:|---:|---:|
| `search_filings` | 23 | 18 | | 15 |
| `calculate_ratio` | 6 | 3 | | 6 |
| `get_stock_price` | 5 | 5 | | 5 |
| `search_filings` + `get_stock_price` | 4 | 3 | | 0 |
| `search_filings` + `calculate_ratio` | 2 | 1 | | 1 |

Cases that moved:

- **Passed on the hosted model only (8):** `nvda-diluted-eps-fy2026`,
  `nvda-revenue-fy2024`, `aapl-gross-margin-dollars-fy2025`,
  `aapl-net-income-fy2025`, `nvda-effective-tax-rate-derived`,
  `nvda-implied-market-cap`, `aapl-implied-market-cap`,
  `aapl-implied-pe-on-fy2025-eps`.
- **Passed on Qwen only (5):** `nvda-gross-margin-pct-mdna`,
  `nvda-effective-tax-rate-mdna`, `aapl-gross-margin-tool`,
  `aapl-net-margin-tool`, `nvda-vs-aapl-gross-margin-gap`.

## Failures by root cause

One label per failed case, by the rule in FAILURE_ANALYSIS.md. Labels for
hosted run 1 are in `benchmarks/results/2026-11-15-groq/labels_run1.json`.

| root cause | hosted run 1 | hosted run 2 | Qwen baseline |
|---|---:|---:|---:|
| wrong tool or wrong arguments | 3 | | 9 |
| retrieval miss | 3 | | 2 |
| grounding-guard refusal | 2 | | 0 |
| extraction or arithmetic error | 2 | | 2 |
| timeout | 0 | | 0 |
| grader error | 0 | | 0 |
| **failed** | **10** | | **13** |

What the hosted model's ten failures are:

- **Wrong tool (3).** All three are Apple fiscal 2026 ratio questions
  (`aapl-gross-margin-tool`, `aapl-net-margin-tool`,
  `nvda-vs-aapl-gross-margin-gap`). The model declined because only Apple's
  fiscal 2025 10-K is indexed, and never called `calculate_ratio`, which holds
  fiscal 2026. Qwen passes all three.
- **Retrieval miss (3).** `aapl-buyback-program` and `aapl-dividend-per-share`
  fail as they do on Qwen: the figure is in no excerpt the search returns.
  `nvda-datacenter-growth` searched the right section, but the returned text
  gives the 59% growth of a sub-line and not the 68% asked for.
- **Grounding-guard refusal (2).** Both drafts were correct.
  - `nvda-gross-margin-pct-mdna`: the draft said 71.07%. The guard flagged
    "10" because the model wrote "Form 10‑K" with a non-breaking hyphen
    (U+2011), which the form rule does not recognise, and "2026" in "its 2026
    Form 10-K". Re-running the unchanged guard on the same draft with an
    ordinary hyphen still flags "2026" but not "10".
  - `aapl-gross-margin-pct-derived`: the draft computed 195,201 / 416,161 =
    46.9% from the FY2025 statement. Both operands are in the tool results.
    The turn held 126 operands, `DERIVATION_LIMIT` is 120, and above the limit
    no derivation is checked, so the figure was refused. Inferred, not
    tested: the statement table is now returned whole, so one more tool result
    in the same turn is enough to pass the limit.
- **Had the inputs, never computed (2).** `nvda-effective-tax-rate-mdna` and
  `nvda-implied-pe-on-fy2026-eps` had every input after one or two calls,
  then called `calculate_ratio` for ratios that do not answer the question
  until the 5-step limit.

Two observations that are not failures in this run but belong with them:

- **35 of the 40 hosted answers contain U+202F (narrow no-break space) or
  U+2011.** Nine turns ended with the `[Unverified]` caveat. Five of those
  still passed, so the answer was right and carried a caveat it did not earn.
- **The guard was not changed.** Both refusals are recorded here as findings.
  A fix is a guard change and needs its own approval and commit.

## Latency

| | hosted run 1 | hosted run 2 | Qwen baseline |
|---|---:|---:|---:|
| p50 | 28.7 s | | 99.4 s |
| p95 | 76.4 s | | 313.5 s |
| mean | 33.8 s | | 125.1 s |
| p50 with the rate-limit wait subtracted | 4.3 s | | not applicable |
| p95 with the rate-limit wait subtracted | 8.8 s | | not applicable |

- The hosted latency as measured is mostly the provider's throttle. This key
  allows 8,000 tokens a minute and a question used 9,409 on average, so the
  proxy waited out a 429 on 39 of the 40 questions: 1,125 s of the run's
  1,353 s.
- The rows with the wait subtracted are **computed, not measured**: the
  recorded wait is taken off each case's latency. They estimate what a key
  without the per-minute limit would give. No run was made on such a key.
- The two sides are different hardware as well as different models: a remote
  accelerator against this laptop's CPU.

## Cost per query

| | hosted run 1 | Qwen baseline |
|---|---:|---:|
| prompt tokens (run total) | 354,190 | 206,770 |
| completion tokens (run total) | 22,162 | 4,746 |
| tokens per question (mean) | 9,409 | 5,288 |
| cost of the run at list price | $0.0664 | not priced |
| **cost per query** | **$0.0017** | not priced |

- Price: $0.15 per million input tokens and $0.60 per million output tokens
  for `openai/gpt-oss-120b`, from Groq's models page
  (`https://console.groq.com/docs/models`) as read on 2026-11-15. The page
  shows no date of its own. Cached input is listed at $0.075; the cost above
  prices every input token at the full rate, so it is an upper bound.
- The run itself was on the free tier and was not billed.
- Qwen is local. Its cost is electricity and time on this laptop, and neither
  was measured.
- The hosted model used 1.7 times the prompt tokens of Qwen for the same 40
  questions. It made 116 model calls against 102, and the step-limit cases
  are expensive (below).

## Where the tokens go

From `token_breakdown_run1.txt` in the results directory, counted over the
116 model calls of hosted run 1.

| part | size | note |
|---|---:|---|
| system prompt | 1,080 tokens | sent on every call |
| four tool schemas | 1,467 tokens | sent on every call |
| fixed part per call, as billed | about 2,430 tokens | from the three 2-call ratio questions (about 5,000 prompt tokens each) |
| model calls per question | 2.9 mean | |
| one `search_filings` result | 1,819 tokens mean, 3,445 max | 39 results |
| one `get_stock_price` result | 497 tokens mean | 9 results |
| one `calculate_ratio` result | 123 tokens mean | 19 results |

The component sizes are counted with tiktoken `o200k_base`, which is close to
but not the provider's tokenizer; the per-call figure is from the provider's
own usage numbers.

- **About 80% of prompt tokens are the fixed part** (116 calls × about 2,430
  = about 282,000 of 354,190). This is inferred from the per-call figure, not
  itemised by the provider.
- **A quarter of all prompt tokens went to the five step-limit failures**
  (87,937 tokens, 17,587 each, against 6,523 for a question that finished
  normally).
- **Another 27% went to the nine turns the guard sent back** (96,653 tokens,
  10,739 each).
- Inside `search_filings` results, table rows are 45% of the tokens, excerpt
  text 39% and per-hit metadata 16%. Each table row carries both `values` and
  `values_absolute` (13,479 tokens for the second across the run), and a table
  hit also carries excerpt text beside its rows (16,665 tokens).

## Why there is one hosted run so far

Run 2 started at 06:28:18 UTC. From case 5 the proxy's waits went to its
75 s cap and cases failed after about 300 s each. A direct request of
question size was refused with:

```
Rate limit reached for model `openai/gpt-oss-120b` ... on tokens per day (TPD):
Limit 200000, Used 197568, Requested 4574. Please try again in 15m25.344s.
```

Run 2 was stopped at 07:13:58 UTC after 12 cases. Its log is kept as
`run_eval2_aborted.log`. Its results measure the throttle and are not used
anywhere in this document. The allowance is a rolling 24 hours, so the second
run is planned for after 06:30 UTC on 2026-11-16.

## Bookkeeping difference between hosted run 1 and run 2

Hosted run 2 will run on code that records, for every model call, how many
prompt tokens the provider reports as served from its prompt cache
(`cached_prompt_tokens` and `call_usage` in the stored turn, and
`cached_prompt_tokens` per case in the eval report). Run 1 was on `1b39944`,
which did not record this. The change adds fields to what is recorded and
nothing else: the prompt, the tools, the model settings, the guard and the
scorer are the same, so the two runs still measure the same system.

Why it was added: whether Groq's prompt cache reduces what counts against the
daily allowance could not be settled from run 1.

- Groq's documentation says "Cached tokens do not count towards your rate
  limits" and lists this model as cached automatically
  (`https://console.groq.com/docs/prompt-caching`, read 2026-11-15).
- Run 1 sent 376,352 tokens, and after it and twelve more questions the
  provider reported 197,568 used of 200,000. That is consistent with about half
  the tokens not counting.
- Three identical requests of the agent's first-call prompt (2,406 prompt
  tokens), sent two seconds apart on 2026-11-15, each returned a usage block
  with no cached tokens (`cache_probe.txt` in the results directory).

## Not done in this step

- **Hosted router with Qwen answering** was dropped at the user's request, so
  how much of the difference is routing is not measured.
- **The hosted run is not the baseline.** `eval/baselines/current.json` is
  still run-c on Qwen.
- **A final Qwen run on the current commit** for a same-code comparison is
  planned for the end of this piece of work.

## Reproducing

```bash
# .env: GROQ_API_KEY set. The API is started with the hosted settings:
#   AGENT_PROVIDER=groq AGENT_MODEL=openai/gpt-oss-120b
#   AGENT_ROUTER_MODEL=openai/gpt-oss-120b AGENT_MAX_TOKENS=2048
python -m eval.run_eval --report benchmarks/results/2026-11-15-groq/eval_run1.json

# traces for one run; --until keeps a later run's turns out
python benchmarks/export_eval_traces.py benchmarks/results/2026-11-15-groq/eval_run1.json \
    --since 2026-11-15T06:05:00Z --until 2026-11-15T06:28:00Z \
    --out benchmarks/results/2026-11-15-groq/traces_run1.json
python benchmarks/render_eval_traces.py benchmarks/results/2026-11-15-groq/traces_run1.json
```

The latency percentiles, the token totals and the cost are computed from the
`outcomes` of the two `eval_run*.json` reports: linear-interpolated
percentiles of `latency_ms`, and of `latency_ms - rate_limit_wait_ms` for the
subtracted rows.
