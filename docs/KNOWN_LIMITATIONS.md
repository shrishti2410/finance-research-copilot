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
