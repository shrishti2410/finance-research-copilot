# Hosted run, 2026-11-18: guard batch + broad questions (fast subset)

**What was run.** The 15-case fast subset (`eval/fast_subset.json`) on
`fix/broad-questions`. This branch has the four guard fixes, the
tool-schema trim and the per-request token budget (`integration/guard-batch`).
It also has the broad-question changes: the snapshot tool, the tool-free
last step, and a last-step `[Unverified]` answer kept and marked inline.

The 5 open-ended cases planned after it **did not run**. The script ran
`eval.open_ended` from the main checkout (master), whose `eval/` package has
no `open_ended.py` and shadowed the branch's. It failed at import, before
any model call. A re-run the same day was not started: the fast subset
alone used about 148,000 counted tokens, leaving too little of the day's
allowance for 5 cases plus the 20,000 headroom.

## Setup

| | |
|---|---|
| agent code | `fix/broad-questions` at `6ac6fd9` (clean tree); the API started with that tree first on `sys.path` (`api.out.log`: "agent code: …/wt-bq/agent/tools.py") |
| eval runner | `python -m eval.run_eval --subset fast`, run from master `e16826d`; `eval/metrics.py`, `run_eval.py`, `judge.py`, `questions.json` and `fast_subset.json` are identical on both commits (`git diff --stat e16826d 6ac6fd9` on them: empty) |
| provider | Groq, `openai/gpt-oss-120b` for answer and router steps, `AGENT_MAX_TOKENS=2048` |
| date | 2026-11-18, 10:05:45–10:23:57 UTC |
| machine | Windows 11 laptop, 16 GB; tools and retrieval local (pgvector, port 55433); standby blocked for the whole run |
| raw output | `benchmarks/results/2026-11-18-guard-broad/` (gitignored): `fast.json`, `fast.log`, `api.err.log`, `run_env.txt` |

## Results (measured)

```
overall: 11/15 = 73.3%
latency   mean    72.5s   median    63.5s   min   13.4s   max   175.0s
tokens    prompt    233,893   completion    9,041   total    242,934
```

| | this run | calculator run, same 15 cases (2026-11-17) |
|---|---:|---:|
| passed | **11/15** | 11/15 |
| verdicts that differ | none | |
| `[Unverified]` banners | **0** | 0 |
| advice replacements (`investment_advice`) | **0** | |
| step-limit exhaustions (`max_iterations`, `tool_call_budget`) | **0** | |
| 413 (request too large) | **0** | 2 in the full 40-case run |
| inference errors | 1 (a 400; below) | |

The calculator column is computed from that run's saved
`benchmarks/results/2026-11-17-calculator/eval_run.json`, restricted to the
same 15 case ids. Same score and the same verdict on every case: **flat**.

**What this subset cannot show.** The four false `[Unverified]` banners of
the calculator run (citation markers read as figures) were on cases outside
these 15. The 0 here is consistent with the fix but does not test it; the
full 40 or the open-ended set does.

### Request sizes (Groq's own counts)

Per model call, from `meta.agent.call_usage` stored with each of the 15
answers (48 calls):

| | tokens |
|---|---:|
| prompt, min | 2,808 |
| prompt, median | 4,657 |
| prompt, p95 | 6,326 |
| prompt, max | 6,584 |
| prompt + completion, max | 6,732 |
| calls over 7,000 | 0 |

The agent's own size log (`agent: request to …`) is at INFO level and the
API's log kept WARNING and above only, so the sizes come from Groq's counts
instead of the estimate.

### Tokens against the daily allowance

Counted (uncached prompt + completion): 233,893 − 94,976 cached + 9,041 =
**147,958**, about twice the ~70,000 assumed for the fast subset.

### Failures

| case | verdict | what happened |
|---|---|---|
| `nvda-datacenter-growth` | wrong | answered 59% from a filing excerpt; expected 68% |
| `aapl-buyback-program` | no_number | the retrieved excerpts did not contain the May 2025 authorization; said so |
| `aapl-dividend-per-share` | no_number | found FY2026's $0.27, not the May 2025 amount; said it could not establish it |
| `nvda-vs-aapl-gross-margin-gap` | no_number (`inference_error`) | iteration 5, the tool-free last step: the proxy returned **400 Bad Request** |

The same four failed in the calculator run.

**The 400 on the last step.** The 400's body was not logged. The likeliest
cause (inferred, not confirmed) is that the model emitted a tool call on the
step sent without tools. Groq rejects a tool call that is not in the
request. Three other cases reached iteration 5 and answered normally. The
proposed handling: a failed tool-free last step falls back to the readable
findings rather than an infrastructure error, and the 400's body is logged.
It is not yet built.
