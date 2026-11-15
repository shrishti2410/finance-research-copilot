# Eval run-c after the five fixes: 27/40, and what the 13 failures are

**Result: 27/40 = 67.5%** (5 wrong, 8 no_number, 0 request errors). Run-b
scored 15/40 as saved and scores 18/40 = 45.0% when its stored answers are
rescored against today's questions and scorer. Against that rescored figure
the change is **+9 cases, with none lost**. This run is now
`eval/baselines/current.json` (`run-c-2026-11-11`).

Every number here comes from the commands in [Reproducing](#reproducing). Raw
outputs are in `benchmarks/results/2026-11-11-eval/` (gitignored). The
failure categories and the rule for assigning them are the ones in
[FAILURE_ANALYSIS.md](FAILURE_ANALYSIS.md).

## How it was run

| | |
|---|---|
| commit | `d3125b4` (master: deadline tests, guard ×2, excerpt cut, reworded questions, final-figure grader, routing hints) |
| command | `python -m eval.run_eval --report benchmarks/results/2026-11-11-eval/eval_run.json` |
| started / finished (UTC) | 2026-11-11 11:19:32 → 12:43:04 (84 min) |
| machine | Windows 11 laptop, CPU only; standby blocked for the whole run |
| inference | Ollama 0.40.16, `qwen2.5:7b` (answers) + `qwen2.5:1.5b` (router), same digests as run-b |
| index | unchanged from run-b: AAPL FY2025 (41 chunks), NVDA FY2026 (72 chunks) |
| questions | 40 cases; 6 reworded to name their fiscal year, 4 of those with regenerated values (`96e74e9`) |

## Which number to compare against

Run-b's 37.5% is not the comparison point, because two things changed that
are not the agent:

- **The questions.** Three Apple tool cases had stale expected values. Run-b's
  own answers to them (48.79%, 27.60%, 0.71x) were right, and they pass once
  the values are regenerated.
- **The scorer.** It now reads the agent's `Final figure:` line first. Run-b's
  stored answers have no such line, so they rescore with the prose regex
  exactly as before.

`eval.compare` rescores both sides with the current questions and scorer:

| | passed | accuracy |
|---|---:|---:|
| run-b as saved (2026-11-10) | 15/40 | 37.5% |
| run-b rescored today | 18/40 | 45.0% |
| **run-c** | **27/40** | **67.5%** |

Nine cases newly pass and none stopped passing. The noise band for one run is
2 net cases, so the direction is established; the size (+9) is one run's
reading, not a mean.

## Accuracy by failure category, against rescored run-b

Each row follows the cases that run-b failed for that reason.

| run-b root cause | run-b failures (rescored) | recovered in run-c | still failing |
|---|---:|---:|---:|
| wrong tool or wrong arguments | 15 | **6** | 9 |
| retrieval miss | 6 | **2** | 4 |
| grader error | 1 | **1** | 0 |
| extraction, guard refusal, timeout | 0 | – | – |
| **total** | **22** | **9** | **13** |

The 13 run-c failures, by their own root cause:

| run-c root cause | cases | wrong | no_number |
|---|---:|---:|---:|
| wrong tool or wrong arguments | **9** | 5 | 4 |
| retrieval miss | **2** | 0 | 2 |
| extraction or arithmetic error | **2** | 0 | 2 |
| grounding-guard refusal | 0 | 0 | 0 |
| timeout | 0 | 0 | 0 |
| grader error | 0 | 0 | 0 |
| **total** | **13** | **5** | **8** |

Four of the still-failing cases changed category, which is why the two tables
do not line up row by row:

- **Wrong tool → retrieval miss (2):** the Apple buyback and dividend cases
  now search the filing and get the right chunk, and lose the answer to the
  prose cut.
- **Retrieval miss → arithmetic (2):** both market-cap cases now reach the
  share count and multiply wrongly.
- **Retrieval miss → wrong arguments (2):** NVIDIA FY2024 revenue and Data
  Center growth now fail on how the search was called.

By `answerable_via`, from `eval.compare`:

| category | run-b rescored | run-c |
|---|---|---|
| `search_filings` | 7/23 | 15/23 |
| `calculate_ratio` | 5/6 | 6/6 |
| `get_stock_price` | 5/5 | 5/5 |
| `search_filings + calculate_ratio` | 1/2 | 1/2 |
| `search_filings + get_stock_price` | 0/4 | 0/4 |

## What recovered, and which fix did it

| case | run-b cause | what changed in the trace |
|---|---|---|
| aapl-basic-shares-fy2025 | retrieval miss | the statement arrives whole; the model reads 14,948,500 thousand shares |
| aapl-services-fy2025 | retrieval miss | searched `section="income"` and read Services 109,158 under Net sales |
| nvda-diluted-shares-fy2026 | wrong tool | after the `unsupported` message, searched the statement and read 24,514 |
| aapl-basic-eps-fy2025 | wrong tool | reached the statement (4 tool calls) and read 7.49 |
| aapl-net-income-fy2023 | wrong tool | searched the statement and read the FY2023 column, 96,995 |
| aapl-services-share-derived | wrong tool | Services and total net sales from the statement; stated 26.30% |
| nvda-revenue-growth-derived | wrong tool | both revenue columns from the statement; stated 65.42% |
| nvda-effective-tax-rate-mdna | wrong arguments | found the tax figures and stated 15.12% |
| nvda-vs-aapl-gross-margin-gap | grader | scored from its `Final figure:` line, 22.28, against the regenerated value |

Eight of the nine depend on the statement being visible (the excerpt fix),
and six of those also needed the agent to go to the filing at all (the
routing hints). The fixes were merged together, so their separate
contributions are read off the traces, not measured.

## The 13 failures

| # | case | verdict | expected → got | root cause | what the trace shows |
|---:|---|---|---|---|---|
| 5 | nvda-diluted-eps-fy2026 | no_number | 4.90 → – | wrong tool | `calculate_ratio(net_margin)`; never searched; answered net margin |
| 6 | nvda-revenue-fy2024 | no_number | 60,922 → – | wrong arguments | `search_filings(section="income")` with no `query` → `bad_input`; then a ratio call; concluded FY2024 is not indexed |
| 10 | aapl-gross-margin-dollars-fy2025 | wrong | 195,201 → 233,600 | wrong tool | question names FY2025; ratio tool returned FY2026 |
| 11 | aapl-net-income-fy2025 | wrong | 112,010 → 132,180 | wrong tool | same |
| 18 | nvda-datacenter-growth | no_number | 68 → – | wrong arguments | searched `section="income"`; the 68% sentence is in MD&A |
| 19 | aapl-buyback-program | no_number | 100 → – | retrieval miss (cut) | right chunk at rank 1; "$100 billion" is at char 1,298 |
| 20 | aapl-dividend-per-share | no_number | 0.26 → – | retrieval miss (cut) | right chunk at rank 1; "$0.26" is at char 1,103 |
| 22 | nvda-effective-tax-rate-derived | no_number | 15.12 → – | wrong arguments | `search_filings` with no `query`, three times; `max_iterations` |
| 24 | aapl-gross-margin-pct-derived | wrong | 46.91 → 48.79 | wrong tool | question names FY2025; ratio tool returned FY2026 |
| 36 | nvda-implied-market-cap | no_number | 5,490 → – | arithmetic | 223.71 × 24,514 written as 5,499,999.94; the true product is 5,484,026.94 |
| 37 | aapl-implied-market-cap | no_number | 4,697 → – | arithmetic | 15,004,697,000 × 313.06 written as 4,799,999,999,996; true 4,697,370,442,820 |
| 38 | nvda-implied-pe-on-fy2026-eps | wrong | 45.71 → 21.35 | wrong tool | live trailing P/E instead of Aug-7 close ÷ FY2026 EPS |
| 39 | aapl-implied-pe-on-fy2025-eps | wrong | 41.97 → 38.81 | wrong tool | same |

## The three cases that moved from wrong to no_number

The question was whether the agent declined honestly or the `Final figure:
none` line hid a figure that the prose stated.

- **nvda-diluted-eps-fy2026: the line hid prose figures, but none was an
  EPS.** The agent answered a different question ("the net margin was
  55.60%, net income $120.07 billion") and wrote `Final figure: none`. The
  prose regex would have scored $120.07 billion as the EPS, a wrong. The
  line is accurate (there is no EPS in the answer); the prose is not an
  honest decline, because it never says it could not find the EPS.
- **nvda-datacenter-growth: an honest decline on the percentage.** The answer
  says it cannot compute the growth and states no percentage, so the prose
  regex also finds nothing. It does contain an error: it calls total revenue
  ($215,938 million) Data Center revenue.
- **nvda-effective-tax-rate-derived: no line at all.** The loop ran out of
  iterations after three identical failed searches. A draft that invented a
  $10.2 billion net income and a 20.6% rate was discarded by the guard, and
  the user got the standard "could not complete" message. That is the guard
  and the budget working; nothing was hidden.

In none of the three does the `none` rule change pass/fail.

## One case where the `none` rule does change the verdict

**nvda-implied-market-cap** would have scored a **pass** under the old prose
regex. The final answer says "the implied market capitalization is $5,500
billion", which is inside the case's ±164.7 tolerance of 5,490.16. It scored
no_number because the answer ended `Final figure: none`.

The answer carried an `[Unverified]` prefix: its arithmetic was wrong
(5,499,999.94 against 5,484,026.94), and the guard said so. The agent then
declined in the line and kept the figure in the prose. Scoring it as
no_number is consistent with what the agent and the guard both said. Scoring
the prose would make this run 28/40, by passing an answer flagged unverified.
The rule stays as approved; this is the case to look at if it is revisited.

## New findings from this run

**1. The routing hint's example call is incomplete.** The new `unsupported`
message says the figures are "via `search_filings(section="income")`". In two
cases (#6, #22) the model copied that call literally, without the required
`query`, and got `bad_input`; in #22 it repeated the same call three times
and ran out of iterations. The hint sent the agent to the right tool and gave
it a call that cannot run. Writing the example with a query
(`search_filings(query="income tax expense", section="income")`) is the
obvious fix, and making `query` optional when a section is given is the
sturdier one. Not done.

**2. The model cannot be trusted to multiply.** Both market-cap cases had the
right price and the right share count and wrote a wrong product, each a
suspiciously round number (5,499,999.94; 4,799,999,999,996). The guard caught
both. The existing known limitation proposes that derived figures come from a
calculator tool; these two cases are the evidence for it.

**3. The prose cut now costs two cases outright.** Buyback and dividend
retrieve the right chunk at rank 1 and lose the sentence at characters 1,298
and 1,103. `nvda-datacenter-growth` has the same problem behind its
wrong-section search (the sentence is at char 828). The excerpt fix exempted
tables and left prose at 700 characters on purpose; this is its cost.

**4. Five failures are the latest-period ratio tool answering a question
about another period** (#10, #11, #24, #38, #39). The description now says
the tool returns only the most recent period, and the model used it anyway.
The cheap routing hints did not move these; the fiscal-year parameter planned
for the structured-facts tool is the fix.

## Cost

| | run-b | run-c |
|---|---:|---:|
| mean latency per case | 125.3 s | 125.1 s |
| median latency per case | 70.8 s | 99.4 s |
| total tokens (measured calls) | 174,771 | 211,516 (+21%) |
| tool calls | 60 | 60 |

Mean latency is unchanged across the 40 cases and the median rose by 29 s.
More cases now search the filing and read a full statement, which is where
the extra tokens go. This is one run on each side.

## What would recover the most now

Read off the traces; each needs a run to confirm.

1. **Fix the hint's example call** (#6, #22, likely #18's section choice
   too): up to 2–3 cases, a one-line change.
2. **A calculator tool, or arithmetic done by the guard's own check** (#36,
   #37): 2 cases.
3. **Stop cutting the sentence that matched** (#19, #20, #18): centre the
   prose excerpt on the best-matching sentence instead of taking the first
   700 characters. Up to 3 cases.
4. **Fiscal-year parameter on structured facts** (#10, #11, #24, #38, #39,
   #5): up to 6 cases; this is the next phase's work.

## Verification

The labels are in `results/2026-11-11-eval/labels.json`. A script joined them
to `traces.json` and to run-b's labels and recomputed every count above: 13
failed cases, 13 labelled, verdicts equal; 9 / 2 / 2 by cause; run-b rescored
at 18 passed with its 22 failures splitting 15 / 6 / 1; 6 + 2 + 1 recovered;
no case that passed in rescored run-b fails now. The buyback and dividend
ranks and character offsets were measured by running the agent's own queries
against the index. The labels were written by the same reader as run-b's, so
this checks consistency, not independence.

## Reproducing

```bash
python -m eval.run_eval --report benchmarks/results/<date>-eval/eval_run.json
python benchmarks/export_eval_traces.py benchmarks/results/<date>-eval/eval_run.json \
    --since <run start, ISO UTC> --out benchmarks/results/<date>-eval/traces.json
python -m eval.compare --report benchmarks/results/<date>-eval/eval_run.json
python benchmarks/render_eval_traces.py benchmarks/results/<date>-eval/traces.json
```
