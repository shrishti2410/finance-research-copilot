# Excerpt cut: tables are never truncated (fix 2, 2026-11-11)

**Change:** `agent/tools.py`, commit `439f893`. A `search_filings` hit whose
chunk is a table is returned whole: caption, units note and every row. Its
parsed rows (`label`, `kind`, `values`, `values_absolute`) ride along in the
full result for the trace and the grounding guard. Prose keeps the
700-character limit but is cut at a sentence end and marked `…`.

**Why:** in eval run-b the cut showed the model only the caption of each income
statement, so every statement figure was invisible. Five failures traced to it
([FAILURE_ANALYSIS.md](FAILURE_ANALYSIS.md)).

Every number below comes from a command run on 2026-11-11 on the Windows CPU
laptop. The setup was Ollama 0.40.16 with `qwen2.5:7b` answering and
`qwen2.5:1.5b` routing, the API at commit `439f893`, and standby blocked.
Raw outputs are in `benchmarks/results/2026-11-11-fixes/` (gitignored).

## Prompt size

This is the `search_filings` tool message the model sees
(`wrap_tool_result(compact_for_model(...))`, k=3) against the local index,
counted with the Qwen2.5 tokenizer. Before is commit `d25f11a`, after is
`439f893`.

| query | before chars / tokens | after chars / tokens | change |
|---|---|---|---|
| NVDA "diluted share count" (statement at rank 1) | 2,752 / 678 | 5,030 / 1,269 | **+591 tokens (+87%)** |
| AAPL "Services revenue fiscal 2025" (statement at rank 3) | 2,757 / 867 | 5,232 / 1,563 | **+696 tokens (+80%)** |
| NVDA "main competitive risks" (prose only) | 2,619 / 571 | 2,283 / 528 | −43 tokens (−8%) |

The cost appears only when a statement is retrieved: about 600–700 extra
prompt tokens per search call that returns one. Prose-only searches get
slightly smaller, because the cut now ends at a sentence. The parsed rows stay
out of the model's message, since they repeat the grid's figures.

## Affected cases, re-run

The affected cases are the six run-b cases whose searches returned the
statement chunk, each run once. Command:
`python -m eval.run_eval --only <6 ids> --report benchmarks/results/2026-11-11-fixes/excerpt_rerun.json`.

| case | run-b | now | latency run-b → now |
|---|---|---|---|
| nvda-rnd-fy2026 | pass | pass | 188 s → 318 s |
| aapl-revenue-fy2025 | pass | pass | 220 s → 261 s |
| aapl-services-fy2025 | wrong | wrong | 213 s → 240 s |
| aapl-basic-shares-fy2025 | no_number | **pass** | 456 s → 251 s |
| nvda-implied-market-cap | no_number | wrong | 263 s → 275 s |
| aapl-implied-market-cap | no_number | no_number | 287 s → 263 s |

**One case recovered:** `aapl-basic-shares-fy2025` now passes, because the
model read 14,948,500 thousand shares from the whole statement. None of the
six regressed.

**Latency is not established.** These are single runs of six cases, and they
move in both directions. Two cases are slower by 41 s and 130 s, and two are
faster by 24 s and 205 s; the faster ones are the cases that no longer loop
through searches looking for a number. Taking about 650 extra prompt tokens at
the prefill rate this host has shown would predict a few seconds per call, not
minutes. These runs cannot separate that effect from run-to-run variation:
the same question has taken 69 s on one run and 471 s on another on this host,
depending only on which tools the agent tried first
([LOAD_TEST_RESULTS.md](LOAD_TEST_RESULTS.md)). The full 40-case eval after the merge is the measurement to quote.

## Why the other three still fail

**aapl-services-fy2025 (wrong: 82,314 instead of 109,158).** The statement
arrived whole at rank 2, with `Services 109,158` under `Net sales:`. The model
answered with `Services82,314` from rank 1, the MD&A *gross-margin* table
("Gross margin: Products $112,887 Services 82,314"), and named that section as
its source. It read the Services gross-margin row as Services revenue.

*Would clearer row labels in the rendered table fix it?* Only partly, and
probably not this case:

- Rendering the statement's rows with their group ("Net sales: Services",
  "Cost of sales: Services") would remove an ambiguity inside the statement,
  where "Services" appears twice.
- But the misread row is in the MD&A chunk, a prose chunk the renderer does not
  touch, and it outranked the statement.
- A stronger lever is the table caption. Its "Most recent period" line lists
  only totals (net sales, gross margin, operating income, net income). Adding
  segment net sales there, and ranking the statement above MD&A for
  revenue-seeking questions, would put the right figure first.

Neither is implemented. The structured-facts tool planned for the next phase
answers this kind of question by line item instead.

**nvda-implied-market-cap (wrong: 62.999 instead of 5,490).** The agent passed
`section="mdna"`, so the search excluded the statement, and it never saw the
24,514 million diluted shares. It then used "we repurchased 282 million shares"
as the share count and stated 282 M × $223.71 = $62.999 billion.

- **Not a grader error:** the scorer read the $62.999 billion the answer stated.
- **What the guard did:** it flagged `62,999,020,000` and the answer went out
  with an `[Unverified]` prefix. But it flagged it because the price had been
  rounded (223.71 against the tool's 223.7097), not because a buyback count is
  not a share count; with the exact price, this wrong figure would have passed.
  This is now recorded in
  [KNOWN_LIMITATIONS.md](../docs/KNOWN_LIMITATIONS.md).

**aapl-implied-market-cap (no_number).** Unchanged. The run never reached a
share count.
