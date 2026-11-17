# Known limitations

Behaviour that is **deliberate and verified**, not a bug waiting to be found.
Each entry says what happens, why it is that way, what the alternative would
cost, and what evidence would justify revisiting it.

Things that are simply not built yet belong in `PROJECT.md` §6, not here.

---

## Comparison follow-ups re-call a tool instead of reusing prior-turn figures

**What happens.** In a multi-turn conversation, a follow-up that only compares
figures already established earlier in the thread still costs one tool call.

Measured on 2026-09-07, a real three-turn conversation through `POST /ask`:

| turn | message | tool calls |
|---|---|---|
| 1 | "What is NVIDIA's gross margin?" | `calculate_ratio(NVDA)` |
| 2 | "What about Apple's?" | `calculate_ratio(AAPL)` |
| 3 | "Which one is higher?" | `calculate_ratio(AAPL)` again |

Turn 3 resolves both referents correctly from conversation memory — its first
attempt reads *"NVIDIA's gross margin of 71.07% ... is higher than Apple's
46.91%"*, which is right — and the loop discards that answer anyway, re-runs
`calculate_ratio` for AAPL, and answers from the fresh result. The user-visible
answer is correct either way. The cost is one redundant call and roughly one
extra model round-trip, ~20 s on the CPU host.

**Why.** The grounding guard in `agent/orchestrator.py` rejects any answer that
states a figure when no tool call succeeded **in the current turn**. Figures
carried over from an earlier turn's prose do not count.

That rule exists because the alternative is precisely the reasoning that
produces hallucinated numbers. When conversation memory first landed, the model
resolved "What about Apple's?" correctly and then answered it from parametric
memory: **38.03%, against an actual 46.91%** — wrong by nine points, with no
tool call at all. It did that because it had just seen a question of that shape
answered, and imitated the answer rather than the method. "The figure looks
established in the history" is the same inference, and it cannot distinguish a
figure a tool returned from one the model invented two turns ago and has since
been repeating.

So the guard trusts a narrow, checkable thing — *a tool returned this, in this
turn* — rather than a plausible-looking one.

**What it would take to fix properly.** Not loosening the rule, but widening
what counts as verified: every assistant message stores its trace in
`messages.meta`, so a figure can be checked against the tool results recorded
for **this conversation** rather than against the prose. That is a real
provenance mechanism — matching claims in an answer to values in a stored trace
— and it is more machinery than the problem currently justifies.

**When to revisit.** When redundant calls become a measurable latency or cost
problem at scale: many concurrent conversations, a metered tool, or comparison
follow-ups turning out to be a large share of real traffic. Not before. One
extra call on a follow-up is a cheap price for never restating an unverified
number, and the failure it prevents is the kind a user cannot catch.

**Where the code is.** The guard, its constants, and the record of the three
approaches tried (prompt rule, `tool_choice: "required"`, post-hoc catch) are in
`agent/orchestrator.py`. `tests/test_orchestrator.py` pins the behaviour;
`agent/memory.py` covers the history window it interacts with.

---

## Repeated refreshes during an answer each start a run that completes

**What happens.** `POST /ask/stream` runs the agent in a task that is
deliberately **not** cancelled when the client disconnects. A user who asks a
question and then refreshes three times before it finishes has started four
runs. All four complete, and all four store a user message and an assistant
message, so the thread ends up with the same question answered four times.

Each run costs what any run costs: model time (35–190 s on the CPU host), live
tool calls against yfinance and RSS, and two rows.

**Why.** Because the alternative was measured, and it was worse. When the run
*was* tied to the request, hanging up mid-answer produced this:

```
  t= 199.7s  tool_result   calculate_ratio(NVDA, gross_margin)  ok=True
  t= 199.7s  >>> CLOSING THE CONNECTION <<<
  RESULT: after 4 minutes the thread still holds 0 message(s).
```

A completed tool call, three minutes of model time, and the user's own question
— all discarded, because the response generator closing cancelled the task. The
reload looked clean, which is precisely what made it bad: nothing on screen
said the answer had been thrown away rather than never asked for.

Losing a finished answer is a worse failure than doing redundant work. A
duplicate turn is visible, harmless and cheap to delete; a silently dropped one
is neither visible nor recoverable.

The run therefore owns a database session of its own (`_run_and_store` in
`api/routes.py`) rather than the request's, which is closed once the response
ends — writing through that session after a disconnect fails on a closed
connection, which is the mechanism by which the answer used to vanish.

**What it would take to fix properly.** In-flight deduplication keyed on the
conversation: a second `/ask/stream` for a conversation that already has a run
going attaches to that run's event stream instead of starting another. That
gives a refreshing user the *same* answer streaming again, which is what they
actually expect, and stores one turn.

It needs a registry of live runs keyed by conversation id, fan-out from one
queue to several readers, and a decision about what a genuinely different
question submitted during an in-flight run should do (almost certainly: queue,
not join). That is a real piece of concurrency work, and it is not worth
building against a single-user development deployment where the failure it
prevents is a duplicated row.

**When to revisit.** When duplicate runs cost something measurable: a metered
model or tool API, enough concurrent users that redundant runs contend for the
one-at-a-time Ollama backend, or threads polluted badly enough that the
duplicates are a support problem. On this host, with Ollama serving one request
at a time, a refresh storm is self-limiting — the runs queue behind each other
rather than multiplying load.

**Where the code is.** `api/routes.py`: `ask_stream` (which no longer cancels,
and why), `_run_and_store` (the independent session), and `_RUNS` (which keeps
the tasks from being garbage-collected mid-flight). `tests/test_ask_endpoint.py`
pins all three, including that finished runs are released from the registry.

---

## With several years of a company indexed, retrieval returns the wrong year

**What happens.** The step-4 corpus holds three 10-Ks per company. The retrieval
eval's hit@5 fell from 100.0% on the two eval filings to 75.9% on the 45-filing
corpus (`benchmarks/STEP4_CORPUS.md`). "NVIDIA's R&D in fiscal 2026" comes back
with the FY2024 and FY2025 statements: the three years are written in nearly the
same words, and neither the vector search nor the re-rank knows which year a
question asks about.

**Why.** Nothing reads the fiscal year out of the question. `search_filings` has
a `fiscal_period` filter, but the agent is not told to use it, and the retrieval
eval applies no filter at all.

**What it would take to fix properly.** Read the year a question names and pass
it as `fiscal_period`, or boost chunks whose `fiscal_period` matches in the
re-rank. Measure with `python -m eval.retrieval_eval` on the corpus.

**When to revisit.** Before the step-4 corpus replaces the eval's two filings.

**Where the code is.** `rag/retriever.py` (`search_filings`, `rerank`).

---

## Some 10-Ks keep the MD&A or the statements outside the Item headings

**What happens.** JPMorgan's 10-K says its MD&A "appears on pages 46–160" of
the annual report, a separate exhibit. Exxon Mobil's Item 7 refers to "the
Financial Section of this report", a part of the document with no Item heading.
Both MD&As index as 3 chunks. JNJ, JPM, PG and XOM have no income-statement
table chunk. Their figures are still available from the facts tool.

**What it would take.** Follow the cross-reference: ingest Exhibit 13 for
JPMorgan, and parse Exxon's Financial Section as a section of its own.

**Where the code is.** `ingestion/parser.py`; the coverage table is in
`benchmarks/STEP4_CORPUS.md`.

---

## The calculator computes what it is asked to, not what the question needs

**What happens.** Since 2026-11-16 every derived figure comes from the
`calculate` tool, and the grounding guard accepts a figure only if a tool
result contains it (`agent/grounding.py`, `tools/calculator.py`). The model no
longer does arithmetic, and an operand has to be a figure a tool returned or a
fact reference the tool looks up. What the guard still does not judge is the
choice of operands and operation. On the Qwen eval of 2026-11-15 a model
divided net income by pretax income and called the result a tax rate. Through
the calculator that is `percent_of(net_income, pretax_income)`: real operands,
exact arithmetic, a figure in a tool result, and the wrong answer.

**Why.** Which two figures make a tax rate is knowledge about finance, not
about where a number came from. The guard checks provenance.

**What it would take to fix properly.** Named recipes for the common derived
figures (effective tax rate, growth, share of revenue, implied market value,
P/E at a date), each fixing its operands, in the way `calculate_ratio` fixes a
margin's. The free-form calculator would remain for everything else.

**When to revisit.** When an eval shows wrong-operation answers passing the
guard. The failure classification of each run says how many there were.

**Closed by this change.** The guard used to accept a figure one arithmetic
step from two figures in the evidence, so an invented "22%" passed when
retrieved text said "compensation rose 29% while headcount grew 7%". A figure
now has to be in a tool result, and 22 is in none
(`tests/test_grounding_derivation.py::
test_a_round_figure_equal_to_two_unrelated_percentages_is_flagged`). The old
rule is kept as `unsupported_figures_reconstructing`, unused by the agent
loop, until the new one has been measured on a full eval.

**Where the code is.** `tools/calculator.py`; `agent/grounding.py`
(`unsupported_figures`, `ungrounded_operands`); the operand check in
`agent/orchestrator.py` (`_refused_operands`); `tests/test_calculator.py`.

---

## The grounding guard checks that a figure traces to a tool result, not that it is the right figure

**What happens.** The guard verifies provenance: every figure in an answer
must come directly, or by one arithmetic step, from figures that a tool call
returned in this turn. It does not verify that the figures used are the ones
the question needs.

Eval case nvda-implied-market-cap, re-run on 2026-11-11, shows the gap. Asked
for an implied market cap from the diluted share count and a closing price,
the agent searched with `section="mdna"`, so the search excluded the income
statement. It found "we repurchased 282 million shares", used that as the
share count, and stated 282 M × $223.71 = $62.999 billion. The real figure is
24,514 M diluted shares, about $5.49 trillion.

On that run the guard did flag the figure, but only because the agent had
rounded the price (223.71 against the tool's 223.7097). Had it used the exact
price, the product of a buyback count and a share price would have passed.
Both operands are real figures from tool results, and the arithmetic is right.

The guard has no notion of what a number *is*. A buyback count, a share count
and a headcount are all "a number of shares-like things in a tool result", and
nothing in the evidence says which one a market cap needs.

**Why.** Provenance is checkable mechanically. Semantic fitness is not
checkable from the text: telling "282 million shares repurchased" from "24,514
million diluted shares" needs the meaning of each figure, and the guard sees
values.

The first instance of this class, an item number used as an operand, was
narrowed on 2026-11-11 (see the entry above on round invented figures). That
change made "what counts as an operand" stricter. It cannot make "which
operand is right" checkable.

**What it would take to fix properly.** Structured facts, planned for the next
phase (step 3 of the plan): a tool that returns line items by name and fiscal
year (`diluted_weighted_average_shares`, FY2026) with their units. A figure is
then traceable to a *named* fact, and a check can require that a market cap's
share operand is a share-count fact rather than any number of shares that
appeared in the turn.

**When to revisit.** Now possible, not yet done. The structured-facts tool landed on
2026-11-15 (`get_financial_fact`, [STRUCTURED_FACTS.md](STRUCTURED_FACTS.md)): a share
count is now available as a named fact with its unit and filing. The guard does not
use that yet. It still checks that a figure traces to a tool result, not that the
operand was the right kind of fact. That check is a guard change and needs its own
approval.

**Where the code is.** `agent/grounding.py` (`unsupported_figures`,
`derivation_operands`). The case above is in
`benchmarks/results/2026-11-11-fixes/excerpt_rerun_traces.json` and is written
up in `benchmarks/EXCERPT_CUT.md`.
