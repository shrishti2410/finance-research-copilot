# Eval failure analysis: run-b, 2026-11-10

**Result: 15/40 = 37.5%** (18 wrong, 7 no_number, 0 request errors), against the
previous baseline's 18/40 = 45.0%. This run is now `eval/baselines/current.json`
(`run-b-2026-11-10`); see [the baseline note](#the-baseline-note) for why the
drop is not a code regression.

Nothing was fixed. This document says what failed, why, and which change would
recover the most cases. Every number here comes from the commands in
[Reproducing](#reproducing); the raw run is under
`benchmarks/results/2026-11-10-eval/` (gitignored, kept on the machine).

## How it was run

| | |
|---|---|
| commit | `cea0dd7` (master HEAD; the branch adds only the two trace scripts) |
| command | `python -m eval.run_eval --report benchmarks/results/2026-11-10-eval/eval_run.json` |
| started / finished (UTC) | 2026-11-10 13:26:48 → 14:50:29 (84 min) |
| machine | Windows 11 laptop, CPU only; standby blocked with `SetThreadExecutionState` for the whole run; no reboot or standby inside it |
| inference | Ollama **0.40.16**, `qwen2.5:7b` (answers, digest 845dbda0ea48) + `qwen2.5:1.5b` (router, 65ec06548149). The previous baseline ran on Ollama 0.34.0 |
| settings | max_tokens 512 / router 128, 5 iterations, 8 tool calls per iteration, 900 s per-call deadline, history 10 (recorded by the run itself) |
| services | API on :8000; Postgres 16 on 55432 with the `real[]` fallback (no pgvector); Redis 7 in Docker (`copilot-redis`, 6379) |
| index | AAPL FY2025 `0000320193-25-000079` (41 chunks); NVDA FY2026 `0001045810-26-000021` (72 chunks) |
| mean latency | 125.3 s per question (140.4 s in run-a) |

A first attempt the same day was killed at case 8 by a reboot; nothing from it
is used here. `run_eval` writes its report only at the end, so a killed run
leaves no partial report.

## Where the per-question traces come from

`eval.run_eval` asks `/ask` with `include_trace=True` but writes only the answer
and verdict to its report. The trace itself is stored by the API on every
assistant message (`messages.meta["trace"]`, up to 40 steps), so
`benchmarks/export_eval_traces.py` reads it back for the eval account that made
the run and joins it to each case:

`traces.json` — per case: question, expected answer with unit and tolerance,
verdict and extracted value, `stop_reason`, latency, every step (tool,
arguments, ok, full result, or the discarded draft for guard events), the
retrieved filing chunks flattened out of `search_filings` results with rank and
score, and the final answer. `benchmarks/render_eval_traces.py` prints it as
text; `failed_traces.txt` (25 cases) and `all_traces_full.txt` (40, untruncated)
are what the labels below were read from.

## Classification rule

One root cause per failed case: the **first** step in the chain whose fault, if
corrected, would have let the case pass. Applied in this order:

1. **timeout** — `stop_reason` is `inference_error`, or the request itself failed.
2. **grounding-guard refusal** — the guard discarded a draft whose figure was
   supported, or turned a correct answer into a caveated failure.
3. **grader error** — the answer states the correct figure and the scorer read a
   different number, **or** the expected value is no longer what its named
   source returns.
4. **wrong tool or wrong arguments** — the agent never called the tool that
   holds the answer, or called it with arguments that cannot return it (for
   example the latest-year ratio tool for a question naming an earlier year).
5. **retrieval miss** — the right tool was called, but the chunk, or the part of
   the chunk, holding the answer did not reach the model: not in the top-k, or
   cut off by the 700-character excerpt `agent/tools.py` shows the model.
6. **extraction or arithmetic error** — the evidence holding the answer was in
   front of the model and it misread or miscomputed.

Rule 2 is deliberately narrow: a guard that discards an *invented* number is
working, and such a case is labelled by whatever left the model without the
real number.

## Count table

| root cause | cases | wrong | no_number |
|---|---:|---:|---:|
| wrong tool or wrong arguments | **15** | 12 | 3 |
| retrieval miss | **6** | 2 | 4 |
| grader error | **4** | 4 | 0 |
| extraction or arithmetic error | 0 | 0 | 0 |
| grounding-guard refusal | 0 | 0 | 0 |
| timeout | 0 | 0 | 0 |
| **total failed** | **25** | **18** | **7** |

By `answerable_via`: 16 of the 23 `search_filings` cases failed (12 wrong tool,
4 retrieval miss); 4 of 6 `calculate_ratio` cases failed (3 grader stale, 1
grader stale + extractor); all 5 `get_stock_price` cases passed; 1 of 2
`search_filings + calculate_ratio` failed (wrong tool); all 4
`search_filings + get_stock_price` cases failed (2 wrong tool, 2 retrieval
miss). Counts recomputed by script from `labels.json` against `traces.json`;
see [Verification](#verification).

Five of the six retrieval misses share one mechanism, and it is the finding of
this run: **the 700-character excerpt cut hides every figure in both income
statements.** The statement chunk begins with its generated caption, which is
itself about 700 characters, so the model sees the list of line-item names and
not one number. Measured on the indexed chunks: NVDA revenue 215,938 starts at
char 713, FY2024 revenue 60,922 at 1,202, diluted EPS 4.90 at 2,758, diluted
shares 24,514 at 3,017; AAPL Services 109,158 at 1,421, net income 112,010 at
807, basic EPS 7.49 at 2,907, basic shares 14,948 at 3,150. When the model
wrote "the excerpt lists the line items but does not provide the specific
number", it was reading accurately.

## Per-case labels

| # | case | verdict | expected → got | root cause | what the trace shows |
|---:|---|---|---|---|---|
| 5 | nvda-diluted-eps-fy2026 | wrong | 4.90 → 0.556 | wrong tool | `calculate_ratio(net_margin)`; EPS is not one of the tool's 7 ratios; filing never searched |
| 6 | nvda-revenue-fy2024 | no_number | 60,922 → – | retrieval miss | query `"revenue"`, k=3 returned three MD&A prose chunks; the statement chunk with the FY2024 column was not in the top 3 |
| 7 | nvda-diluted-shares-fy2026 | no_number | 24,514 → – | wrong tool | invented `ratio_name="weighted_average_diluted_share_count"`; after `unsupported` the agent gave up without trying `search_filings` |
| 9 | aapl-services-fy2025 | wrong | 109,158 → 82,314 | retrieval miss (cut) | statement at rank 2 but 109,158 is at char 1,421; the model then took "Services 82,314" from the MD&A *gross margin* table as revenue |
| 10 | aapl-gross-margin-dollars-fy2025 | wrong | 195,201 → 233,600 | wrong tool | question names FY2025; `calculate_ratio` returns only the latest year, now FY2026 (`period_end 2026-09-30`); filing never searched |
| 11 | aapl-net-income-fy2025 | wrong | 112,010 → 132,180 | wrong tool | same as #10 |
| 12 | aapl-basic-eps-fy2025 | wrong | 7.49 → 27.6 | wrong tool | `calculate_ratio(net_margin)` for an EPS question; the answer holds no EPS; the extractor scored the net margin |
| 13 | aapl-basic-shares-fy2025 | no_number | 14.95 → – | retrieval miss (cut) | statement at rank 1 on three searches; 14,948 is at char 3,150 |
| 14 | aapl-net-income-fy2023 | wrong | 96,995 → 132,180 | wrong tool | question names FY2023; answer labels FY2026 data as FY2023 |
| 16 | nvda-effective-tax-rate-mdna | wrong | 15.1 → 21.93 | wrong args | invented ratio, then `search_filings(query="net margin")` for a tax-rate question; the 15.1% sentence never came back. The 21.93% is fabricated; see [guard finding](#a-fabricated-figure-passed-the-guard) |
| 18 | nvda-datacenter-growth | wrong | 68 → 59 | retrieval miss | the chunk with "Data Center revenue … up 68%" (`7:56`) was not in the top 3; the retrieved `7:61` says "Data Center *computing* grew 59%", quoted accurately |
| 19 | aapl-buyback-program | no_number | 100 → – | wrong tool | `get_stock_price` for May 2025 only; the answer says the filing should be searched, and does not search it |
| 20 | aapl-dividend-per-share | no_number | 0.26 → – | wrong tool | same as #19 |
| 22 | nvda-effective-tax-rate-derived | wrong | 15.12 → 55.6 | wrong tool | `calculate_ratio(net_margin)`; answered net margin for a tax-rate question |
| 23 | nvda-revenue-growth-derived | wrong | 65.47 → 71.07 | wrong tool | `gross_margin` three times, then `ratio_name="revenue"` (unsupported), then search `"revenue"` k=2; budget exhausted (`max_iterations`, 598 s) |
| 24 | aapl-gross-margin-pct-derived | wrong | 46.91 → 48.79 | wrong tool | question names FY2025; the tool returned FY2026 and the answer mislabels it "year ending September 30, 2025" |
| 25 | aapl-services-share-derived | wrong | 26.23 → 48.79 | wrong tool | `calculate_ratio(gross_margin)`, irrelevant to a segment share; filing never searched |
| 27 | aapl-gross-margin-tool | wrong | 46.91 → 48.79 | grader (stale) | the tool is right for today: Apple's latest annual is now FY2026. 46.91% was FY2025 when the set was built (2026-09-08) |
| 29 | aapl-debt-to-equity-tool | wrong | 1.34 → 0.71 | grader (stale) | same; 0.71x is FY2026 |
| 30 | aapl-net-margin-tool | wrong | 26.92 → 27.6 | grader (stale) | same; 27.60% is FY2026 |
| 36 | nvda-implied-market-cap | no_number | 5,490 → – | retrieval miss (cut) | close 223.71 fetched correctly; statement at rank 1 (k=1) but 24,514 is at char 3,017; the model invented 1.025 B shares, the guard discarded that draft, the final answer declined |
| 37 | aapl-implied-market-cap | no_number | 4,697 → – | retrieval miss (cut) | same mechanism; invented 17.8 B shares, discarded; `max_iterations` |
| 38 | nvda-implied-pe-on-fy2026-eps | wrong | 45.71 → 171.25 | wrong tool | `calculate_ratio(pe_ratio)` is a live-price TTM P/E (21.65x); the question wants Aug-7 close ÷ FY2026 filing EPS. The extractor also scored the price, 171.25, not the stated 21.65x, which is wrong too |
| 39 | aapl-implied-pe-on-fy2025-eps | wrong | 41.97 → 231.29 | wrong tool | same; stated 39.05x, extractor took 231.29 |
| 40 | nvda-vs-aapl-gross-margin-gap | wrong | 24.16 → 71.07 | grader (stale) | both calls correct and 22.28 = 71.07 − 48.79 is right for today's data; the expected 24.16 uses FY2025 Apple. The extractor also scored 71.07 rather than the stated 22.28; with correct extraction it would still miss on staleness |

Passed (15): nvda-revenue-fy2026, nvda-gross-profit-fy2026, nvda-rnd-fy2026,
nvda-net-income-fy2026, aapl-revenue-fy2025, nvda-gross-margin-pct-mdna,
nvda-buyback-authorization, nvda-operating-margin-derived,
nvda-gross-margin-tool, nvda-debt-to-equity-tool, and all five
`get_stock_price` cases. Of the four NVDA statement figures that passed, three
came from `calculate_ratio`'s `inputs` (revenue, gross profit, net income are
the inputs to the margins), not from the filing; `nvda-rnd-fy2026` and
`aapl-revenue-fy2025` came from MD&A prose chunks that happen to state them.

## Three traces per category

### Wrong tool or wrong arguments (15)

**#5 nvda-diluted-eps-fy2026** — "What was NVIDIA's diluted earnings per share
in fiscal year 2026?"
```
1. it1 ok calculate_ratio {"period":"annual","ticker":"NVDA","ratio_name":"net_margin"}
      {"ok":true,"ratio":"net_margin","value":0.556025,"inputs":{"revenue":215938000000.0,
       "net_income":120067000000.0},"period_end":"2026-01-31", ...}
2. it2 ok final_answer
      NVIDIA's net margin for fiscal year 2026, which ended on January 31, 2026, was 55.60%. ...
      The net income was $120.07 billion and the revenue was $215.94 billion.
```
Expected 4.90. The router chose the ratio tool for a per-share figure the tool
cannot produce, and the 7b model answered the question the tool *did* answer.
The statement chunk holds "4.90" at char 2,758, so even a correct second step
would have hit the excerpt cut.

**#7 nvda-diluted-shares-fy2026** — "What was NVIDIA's weighted average diluted
share count in fiscal 2026?"
```
1. it1 ✗  calculate_ratio {"period":"annual","ticker":"NVDA","ratio_name":"weighted_average_diluted_share_count"}
      {"ok":false,"error":"unsupported","message":"Unknown ratio '...'. Supported: gross_margin,
       operating_margin, net_margin, return_on_equity, debt_to_equity, current_ratio, pe_ratio."}
2. it2 ok final_answer
      The ratio you requested, "weighted_average_diluted_share_count," is not supported by the
      tools available to me. However, I can provide you with other financial metrics ...
```
Expected 24,514. The `unsupported` envelope lists the seven ratios and says
nothing about where share counts live, and the escalated 7b model stopped
there. The same shape recurs in #16 and #23.

**#14 aapl-net-income-fy2023** — "What was Apple's net income in fiscal year
2023?"
```
1. it1 ok calculate_ratio {"period":"annual","ticker":"AAPL","ratio_name":"net_margin"}
      {... "inputs":{"revenue":478834000000.0,"net_income":132175000000.0},
       "period_end":"2026-09-30", ...}
2. it2 ok final_answer
      Apple's net income in fiscal year 2023 (which ended on September 30, 2026) was $132.18
      billion, based on the net margin of 27.60% and revenue of $478.83 billion.
```
Expected 96,995 (the FY2023 column of the FY2025 statement). The tool returned
its only period and said so (`period_end 2026-09-30`); the model wrote "fiscal
year 2023 (which ended on September 30, 2026)" without noticing the
contradiction. Four cases (#10, #11, #14, #24) are this exact shape, and all
four would have *passed* in September, when the tool's latest year happened to
be the one the question named.

### Retrieval miss (6)

**#13 aapl-basic-shares-fy2025** — "What was Apple's weighted average basic
share count in fiscal 2025?"
```
2. it2 ok search_filings {"k":3,"query":"weighted average basic share count","company":"AAPL"}
      [1] AAPL FY2025 Income Statement score=0.7182
          CONSOLIDATED STATEMENTS OF OPERATIONS for Apple Inc. (AAPL), FY2025. Annual financial
          results for the periods ending September 27,2025, September 28,2024, September 30,2023.
          Line items include total revenue, net sales, turnover, the top lin…   (700 chars, ends here)
3. it3 ok search_filings {... "section":"mdna"}        -> three unrelated MD&A chunks
4. it4 ok search_filings {... "section":"income"}      -> the same statement chunk, same 700 chars
5. it5 ok final_answer
      ... The excerpt lists various financial line items, including "share count, weighted average
      shares outstanding," but does not provide the specific number. ...
```
Expected 14.95 billion. The retriever found the right chunk at rank 1 on the
first try and again on the third; the number (14,948,179 thousand) is at char
3,150 of a 3,300-char chunk, and the model was shown the first 700. The agent's
three searches were all reasonable.

**#36 nvda-implied-market-cap** — "Using NVIDIA's fiscal 2026 diluted share
count and its August 7, 2026 closing price, what was its implied market
capitalisation?"
```
1. it1 ok get_stock_price {"ticker":"NVDA","end_date":"2026-08-07","start_date":"2026-08-07"}
      {... "last":{"date":"2026-08-07","close":223.7097}, "trading_days":1 ...}
2. it2 ok search_filings {"k":1,"query":"diluted share count","company":"NVIDIA"}
      [1] NVDA FY2026 Income Statement score=0.7703   (caption only; 24,514 is at char 3,017)
3. it3 ok calculate_ratio {... "ratio_name":"gross_margin"}            (irrelevant, 71.07%)
4. it4 ✗  ungrounded_answer {"figures":["1,025,000,000","228,832,750,000"]}
      ... Let's assume the diluted share count from the income statement excerpt is 1,025,000,000
      shares (this is an approximation based on typical share counts for large tech companies). ...
5. it5 ok final_answer
      The exact diluted share count for NVIDIA's fiscal 2026 is not directly provided in the
      search results. ... I cannot provide a precise market capitalization figure.
```
Expected 5,490 billion. Price right, chunk right, number hidden. The model then
invented a share count, the guard caught it (correctly; neither figure traces
to any result), and the retry declined. This is the guard working as designed
on a failure the excerpt cut created.

**#18 nvda-datacenter-growth** — "By what percentage did NVIDIA's Data Center
revenue grow in fiscal 2026?"
```
2. it2 ok search_filings {"k":3,"query":"Data Center revenue growth","company":"NVIDIA"}
      [1] NVDA FY2026 Item 7 score=0.8291   "... Compute & Networking $193,479 $116,193 ... 67 % ...
          Revenue from Data Center computing grew 59% driven by demand for our Blackwell ..."
      [2], [3]  Item 7 chunks about customer concentration and pricing
3. it3 ok final_answer
      ... the Data Center revenue grew by 59% year over year. Specifically, the filing states:
      "... Revenue from Data Center computing grew 59% driven by demand for our Blackwell
      computing platform."
```
Expected 68 ("Data Center revenue for fiscal year 2026 was up 68%", in chunk
`7:56`). That chunk was not in the top 3; the one that was contains a true
sentence about the *computing* sub-line, which the model quoted verbatim. Not a
misread; the answering sentence never arrived (and sits at char 828 of its
chunk, so it would have been cut as well).

### Grader error (4)

**#27 aapl-gross-margin-tool** — "What is Apple's most recent annual gross
margin?"
```
1. it1 ok calculate_ratio {"period":"annual","ticker":"AAPL","ratio_name":"gross_margin"}
      {... "value":0.487856, "inputs":{"revenue":478834000000.0,"gross_profit":233602000000.0},
       "formatted":"48.79%", "period_end":"2026-09-30" ...}
2. it2 ok final_answer
      Apple's most recent annual gross margin, as of the fiscal year ending September 30, 2026,
      was 48.79%. ...
```
Expected 46.91 ± 0.5, whose `source` is
`calculate_ratio('AAPL','gross_margin','annual') = 46.91%` — as it was on
2026-09-08. Run live on 2026-11-10 the same call returns 48.79% for
`period_end 2026-09-30`: yfinance now carries Apple's FY2026 statements. The
agent did exactly what the case asks and reported the period. #29 and #30 are
the same.

**#40 nvda-vs-aapl-gross-margin-gap** — "How many percentage points higher was
NVIDIA's most recent gross margin than Apple's?"
```
1. it1 ok calculate_ratio {NVDA gross_margin}  -> 71.07%  period_end 2026-01-31
2. it1 ok calculate_ratio {AAPL gross_margin}  -> 48.79%  period_end 2026-09-30
3. it2 ok final_answer
      NVIDIA's most recent gross margin was 71.07%, while Apple's was 48.79%. The difference is
      22.28 percentage points.
```
Expected 24.16 = 71.07 − 46.91. Two correct parallel calls and correct
arithmetic on today's data. Two separate grader problems: the expected value is
stale, and `eval/metrics.py` scored **71.07**, the first percent-shaped number,
rather than the 22.28 the answer concludes with ("The difference is …" matches
none of the extractor's concluding-sentence patterns). Fixing the extraction
alone would still fail the case (22.28 vs 24.16 ± 1.0).

**#30 aapl-net-margin-tool** — "What is Apple's most recent annual net profit
margin?"
```
1. it1 ok calculate_ratio {"period":"annual","ticker":"AAPL","ratio_name":"net_margin"}
      {... "formatted":"27.60%", "period_end":"2026-09-30" ...}
2. it2 ok final_answer
      Apple's most recent annual net profit margin, as of the fiscal year ending September 30,
      2026, was 27.60%. ...
```
Expected 26.92 (FY2025). Same as #27.

### Extraction or arithmetic, grounding-guard refusal, timeout (0 each)

No case was lost to the model misreading evidence it was shown, to the guard
blocking a supported figure, or to a timeout. The two drafts the guard
discarded (#36, #37) both contained invented share counts. The two
`max_iterations` endings (#23, #37) were budget exhaustion after repeated
useless calls, not deadlines; the 900 s per-call deadline never fired.

## The baseline note

Against `run-a` (45.0%, 2026-09-08 answers at commit `787276f`, Ollama
0.34.0), `eval.compare` reports DOWN by 3 net cases: 4 now passing
(aapl-revenue-fy2025, nvda-buyback-authorization, nvda-gross-margin-tool,
nvda-rnd-fy2026) and 7 no longer passing (aapl-debt-to-equity-tool,
aapl-gross-margin-dollars-fy2025, aapl-gross-margin-pct-derived,
aapl-gross-margin-tool, aapl-net-income-fy2025, aapl-net-margin-tool,
nvda-vs-aapl-gross-margin-gap).

**All seven regressions are the Apple fiscal-year rollover**: four are stale
expected values (grader) and three (#10, #11, #24) are the latest-year ratio
tool being asked about FY2025, which was a correct shortcut in September and is
not now. Inside the noise band the comparison already allows (two consecutive
runs of the unchanged system once differed on 5 of 18 cases), there is no
evidence here of the agent or the Ollama upgrade making anything worse. There is
also no evidence that it made anything better.

`eval/compare.py`'s drift check compares `questions.json` with the baseline
file; it cannot see the world moving under a fixed expected value. The
promoted baseline's `note` records the rollover and the Ollama version, and the
question set has **not** been regenerated (rule 3 of `CLAUDE.md`: that is a
separate, approved commit).

## A fabricated figure passed the guard

In #16 the 1.5b router invented a ratio, the 7b model's first draft said
"NVIDIA's net margin was 21.93%" with no tool having returned it, and the
grounding guard discarded that draft, naming 21.93% as unsupported. The retry
ran `search_filings("net margin")`, got three MD&A chunks, and restated
**21.93%** as the answer, this time without a caveat.

Replaying `agent.grounding.unsupported_figures` on the stored steps shows why:
after the search, the evidence set held 25 numbers from the chunk text, and the
one-step derivation rule found `29 − 7 = 22.0`, within the 0.5 % tolerance of
21.93. Two unrelated small integers in prose made an invented percentage
"derivable". This is the weakness the module's own docstring anticipates for
*rescaled* derivations; it applies to unscaled ones too once the evidence has
enough small numbers. It did not change this case's verdict (the answer was
wrong either way), but it is the one place in the run where an unsupported
number reached the user as a reported figure. Logged here, not fixed, and it
belongs in `tests/test_no_fabrication_on_tool_failure.py` before any change to
the guard.

## Which fix would recover the most

These are estimates read off the traces, not measurements. Each needs a full
eval run to confirm, and the band is ±2 cases on a single run.

**1. Show the model the statement's numbers.** `agent/tools.py` cuts every
`search_filings` excerpt at `EXCERPT_CHARS = 700`, and for a table chunk that is
the caption and nothing else. Rendering table chunks from their `structured`
rows, or exempting `content_type == "table"` from the cut, would put every
statement figure in front of the model. Directly addresses the five excerpt-cut
retrieval misses (#9, #13, #36, #37; #6 and #18 also need the chunk to be
retrieved first). Upper bound on its own: **about 4 cases**. More importantly it
is a precondition for the next fix: of the 15 wrong-tool cases, 13 are answered
by the income statement, and routing them to `search_filings` today would land
them on the same 700-character caption.

**2. Route line items, per-share figures and named years to the filing.** The
agent reached for `calculate_ratio` on 15 of 25 failures, including EPS, share
counts, tax rate, a segment's revenue, buybacks, dividends, and any question
naming a fiscal year. Two cheap levers, both prompt-side and both needing
measurement on a 7B model: the `unsupported` envelope should say where the
figure lives ("not a ratio; the income statement in the indexed 10-K has it:
`search_filings(section="income")`"), which bears on #7, #16, #23; and the tool
description and system prompt should say that `calculate_ratio` returns only
the most recent period and that a question naming a fiscal year is a
`search_filings` question, which bears on #10, #11, #14, #24. With fix 1 in
place, the upper bound across the 13 filing-answerable wrong-tool cases is
large; without it, close to zero.

**3. Regenerate the question set** (`python -m eval.build_questions`, with
approval, in its own commit). Recovers the 3 stale tool cases (#27, #29, #30)
and re-bases #40 to 22.28, which would then still need the extractor to prefer
"The difference is …" over the first percent — a grader change, also rule 3.
**3 cases**, and the baseline stops lying about Apple.

Recommended order: 1, then 2, then 3 — with an eval run after each, compared
against this baseline.

## Verification

**Recount.** The labels live in `results/2026-11-10-eval/labels.json` (one
entry per failed case, with the root cause and a one-line reason). A script
joined them to `traces.json` and recomputed every number above: 25 failed cases
in the traces, 25 labelled, none labelled that passed, every verdict in
`labels.json` equal to the one in `traces.json`, and the category and
`answerable_via` counts as printed. The first draft of this document had two
prose slips the recount caught: the wrong-tool row read 11 wrong / 4 no_number
(it is 12 / 3), and the `search_filings + get_stock_price` sentence listed six
causes for four cases. Both were corrected; no label changed.

**Re-read of a random sample.** Eight failed cases were drawn with
`render_eval_traces.py --failed --sample 8 --seed 20261110` and re-classified
from the raw trace by walking the classification rule in order:
nvda-effective-tax-rate-mdna, nvda-implied-market-cap, aapl-net-income-fy2025,
aapl-debt-to-equity-tool, aapl-gross-margin-pct-derived, aapl-buyback-program,
nvda-datacenter-growth, nvda-implied-pe-on-fy2026-eps. All eight came out as
labelled. Two are judgment calls a second reader could reasonably make
differently, and both sit on the wrong-tool side of a boundary:

- **nvda-effective-tax-rate-mdna** is *wrong arguments* here because the
  search query ("net margin") does not describe the answer; a reader who treats
  any `search_filings` call as "the right tool was called" would label it a
  retrieval miss. Either way it is not a guard refusal and not a misread.
- **aapl-gross-margin-pct-derived** is *wrong tool* here because the question
  names FY2025 and the case's primary source is the filing; but its `source`
  field also says "calculate_ratio gives 46.91%", which is no longer true, so a
  reader weighting that clause would call it grader-stale.

Moving both would make the table 13 / 7 / 5 instead of 15 / 6 / 4 and change
no conclusion. The sample was re-read by the same person who wrote the labels,
within the hour, so this checks that the rule was applied consistently; it is
not an independent review.

## Reproducing

```bash
# services: Postgres on 55432, docker compose -f docker-compose.dev.yml up -d redis,
# Ollama with qwen2.5:7b and qwen2.5:1.5b, API on :8000
python -m eval.run_eval --report benchmarks/results/<date>-eval/eval_run.json
python benchmarks/export_eval_traces.py benchmarks/results/<date>-eval/eval_run.json \
    --since <run start, ISO UTC> --out benchmarks/results/<date>-eval/traces.json
python benchmarks/render_eval_traces.py benchmarks/results/<date>-eval/traces.json > failed_traces.txt
python -m eval.compare --report benchmarks/results/<date>-eval/eval_run.json
```

The excerpt-cut positions above come from `filing_chunks` directly
(`SELECT content FROM filing_chunks WHERE content_type='table'`, then
`content.find(figure)` against `agent.tools.EXCERPT_CHARS`), and the guard
replay from `agent.grounding.unsupported_figures(final_answer, steps[:-1])` on
the stored trace of #16.
