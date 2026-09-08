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
