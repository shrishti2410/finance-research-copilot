# Signal explanations on local Qwen: three live runs

Step 7C, phase 4. `market/explain.py` has the agent explain each watchlist
stock's signal through `get_trading_signal`. Everything here was measured on
local Ollama; no hosted provider was used.

**In short:**
- **Speed.** On this laptop's CPU an explanation takes 3.3 to 6.4 minutes,
  and 1 in 5 still timed out in each of the last two runs.
- **No edge.** Every finished explanation said "no edge" in the model's own
  words.
- **Errors.** In the last run, three of the four also said something wrong
  that the guards cannot catch. The verdict sentence is therefore now put first, verbatim
  (`96b329b`). That change came after the last live run and has not itself
  been run live.

## How it was run

| | |
|---|---|
| command | `python explain_run.py <worktree> <out>`: a scratch driver calling `market.explain.explain_signal` per ticker (not committed) |
| server | a local API from the worktree on 127.0.0.1:8000, with `AGENT_PROVIDER` and `AGENT_ROUTER_PROVIDER` set to `ollama` in its environment; the agent calls the model through it |
| models | `qwen2.5:7b` (answer), `qwen2.5:1.5b` (tool selection), Ollama 0.40.16 |
| machine | ASUS Vivobook X1504ZA, Intel Core i5-1235U, Windows 11 (build 26300), CPU only |
| awake | standby blocked with `SetThreadExecutionState` for each run |
| timeouts | `inference_read_timeout` 300 s of silence, unchanged |
| raw output | `benchmarks/results/2026-11-16-explain/` (runs 1 and 2) and `2026-11-17-explain/` (run 3), gitignored |

## The three runs (measured)

| run | code | tool result | finished | seconds per finished stock |
|---|---|---|---|---|
| 1, 17:55 UTC | before `e58dca5`: 4.6k-character tool result | 4,591 chars (SBIN) | **2 of 5**; 3 timed out writing the answer | 384, 336 |
| 2, 18:30 UTC | as committed in `e58dca5` | 2,823 chars (SBIN) | 4 of 5 (DIXON timed out) | 339, 322, 242, 235 |
| 3, 19:18 UTC | `6cd9929` (calendar refit, store fix) | 2.8k | 4 of 5 (COFORGE timed out) | 322, 201, 210, 287 |

Each timeout was a ReadTimeout on the answer step: the 7B model took more
than 300 s to produce its first token.

**Run 1's count was wrong when it ran.** `explain_signal` returned `ok: true`
for all five, wrapping the three timeout messages in the verdict and the
disclaimer. That is fixed in `e58dca5`: an unfinished run is now a failure
with nothing added.

**Run 2 used a price history that Yahoo had returned without the latest
session.** It was fetched at midnight India time, so its SBIN.NS signal is
as of 2026-11-13. Fixed in `f74ef6d`.

## What the finished explanations said (run 3, read against the tool output)

All four said the backtest shows **no edge**, unprompted
(`model_said_no_edge` would be true for each). None named a card file;
`explain_signal` listed the supplied cards. Errors found:

| stock | error | kind |
|---|---|---|
| DIXON.NS | "shows no edge, meaning that the signal did not outperform a simple buy-and-hold strategy", where the signal did outperform it on CAGR (26.8% against 19.1%) and Sharpe (1.08 against 0.65). It is "no edge" because it beat only 89% of random re-timings, not the 95% bar. | wrong reason for the verdict |
| SBIN.NS | "beaten only 5% of 200 random re-timings", where the tool said 0%. The grounding guard let this figure through. | wrong figure |
| SBIN.NS | "+DI ... is not showing an upward trend" and "the 20-day average is below the 50-day and 200-day averages". Neither value was in the tool output (+DI was in fact 38 against −DI 6). | invented facts |
| PREMIERENE.NS | "ADX of 22.02, indicating a strong trend", where the card and the regime rule treat 25 as the threshold for a strong trend. | misread |
| HDFCBANK.NS | none found | |

DIXON's answer also called the coverage cap (0.375) "the confidence", while
the confidence was 0.317.

## What changed because of this (`96b329b`, not yet run live)

- The tool's backtest sentence, with the right figures and the right reason,
  now always opens the explanation. A wrong restatement by the model then sits
  under the correct one rather than standing alone.
- +DI and −DI are back in the tool output.
- **Not changed:** the grounding and advice guards and the system prompt.
  - The SBIN "5%" is a gap in the grounding guard: a misquoted figure passed.
  - In the phase 4 probes, "The stock will likely rise." was not flagged by the
    advice guard.
  - Both are reported for the owner to decide; they are guarded code.

## Not measured

- The explanations on a hosted model (not allowed until approved).
- Latency with a GPU.
- Any explanation on `96b329b`.
