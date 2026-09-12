# Full-stack load test: 1, 5, 10, 20 concurrent users

Measured 2026-09-12 against the host deployment (not containerized — Docker is
not installed on this machine; see `docs/DEPLOYMENT.md` §8). Locust 2.46.5,
10 minutes per level, all users spawned at once, no think time, fresh tokens
before each level.

Milestone 2 asked this question of the raw inference server. This asks it of
everything: auth, the agent loop, tool execution, Postgres, Redis, inference.

## Results

```
 users  tried  done  failed  req/min  total med  total p95  TTFT med
------------------------------------------------------------------------------
     1      6     6       0      0.6        88s       155s       39s
     5      4     4       1      0.4       371s       563s      145s
    10     63     4      61      0.4       403s       584s      240s
    20    284     0     284      nan          -          -         -

Scaling, relative to 1 user
------------------------------------------------------------------------------
 users  throughput  per-user tput   latency      TTFT
     1       1.00x          1.00x     1.00x     1.00x
     5       0.69x          0.14x     4.22x     3.72x
    10       0.66x          0.07x     4.58x     6.16x
    20          nothing completed

Outcomes by stop_reason
------------------------------------------------------------------------------
 users      final_answer   inference_error
     1                 6                 0
     5                 3                 1
    10                 2                 2
    20                 0                 0
```

## What it says

**Throughput does not rise with concurrency. It falls.** 0.6 req/min at one user,
0.4 at five, 0.4 at ten, zero at twenty. Per-user throughput collapses to 0.07x.
Adding users does not add capacity here; it adds queue.

That is not a surprise, and it was measured separately before this run. The
inference server barely batches:

| concurrency | system tok/s | per-stream tok/s | scaling |
|---|---|---|---|
| 1 | 20.2 | 20.2 | 1.00x |
| 2 | 21.4 | 16.1 | 1.06x |
| 4 | 23.1 | 12.6 | 1.15x |

4x the concurrency buys 15% more throughput. Everything above is downstream of
that one fact.

**Latency and TTFT diverge, which locates the wait.** Total latency rises 4.2x at
five users while TTFT rises 3.7x, and by ten users TTFT has risen **6.2x** —
faster than total. Time is being spent before generation starts, not during it.
The request is holding a place in a queue, which is exactly what a non-batching
server produces.

**The first hard failure arrives at five users**, not twenty: one request in four
ended `inference_error`. That is the 300s inference read timeout firing on a call
that waited too long for a slot. By ten users it is half of them.

## The finding that matters most

At **ten** concurrent users the stack stops degrading gracefully and starts
returning `500 Internal Server Error`:

```
25   error event: TimeoutError: QueuePool limit of size 5 overflow 10 reached,
     connection timed out, timeout 30.00
33   500: Internal Server Error
```

At twenty users, in ten minutes, **284 requests were attempted and none
completed** — 231 of the failures were 500s.

Three separate problems, and the inference server is not any of them.

## Follow-up: the connection is no longer held across the run — measured

Findings 1 and 2 below describe the system as first measured. Both have since
been addressed; this section is what changed and what it was worth.

`/ask` and `/ask/stream` now use the database in two short bursts with nothing
held in between — read the history, release, run the agent for minutes holding no
connection, reacquire to write the turn. `get_current_user` was doing the same
thing and is also short-lived now, which is what removed the *second* connection
per streaming request.

Re-measured at the two levels that failed, same harness, same 10 minutes, same
host:

| | attempts | answers | median | QueuePool timeouts | 500s |
|---|---|---|---|---|---|
| **10 users** before | 63 | 2 | 403s | **25** | **33** |
| **10 users** after | 11 | 4 | 376s | **0** | **0** |
| **20 users** before | 284 | 0 | — | **37** | **231** |
| **20 users** after | 33 | 3 | 373s | **0** | **0** |

**Pool exhaustion is gone at both levels**, and 20 users went from *zero*
completed answers to three. Every remaining failure is `inference_error` — the
300s read timeout on a request queued behind the model — which is the bottleneck
this change was never going to touch.

Two numbers worth reading carefully rather than celebrating:

- **Attempts collapsed** (63 → 11, 284 → 33). That is not less throughput; it is
  the disappearance of instant failures. Before, a 500 came back in 30 seconds
  and a load generator with no think time immediately fired another, so most of
  those attempts were the same request failing over and over.
- **Answers per 10 minutes barely moved** (2 → 4, 0 → 3). The stack was never
  database-bound for *throughput*; it was database-bound for *failures*. The
  ceiling this removed was on how many requests could be in flight without
  erroring, not on how fast the model answers.

The concurrency ceiling is now whatever the inference queue and the 300s timeout
allow, not `db_pool_size + db_max_overflow`. Pinned by
`tests/test_ask_endpoint.py::test_no_connection_is_held_across_the_agent_run`,
which fails if a session is ever open across `run_agent` again.

### 1. A database connection was held for the entire agent run — fixed

`/ask` and `/ask/stream` both took `session: AsyncSession = Depends(get_session)`.
FastAPI holds a dependency for the whole request, and for a stream that is the
whole response — minutes. So one Postgres connection is pinned per in-flight
question, for its full duration, while the agent is doing nothing but waiting on
the model.

`db_pool_size=5` + `db_max_overflow=10` = **15 connections**. That was the hard
concurrency ceiling of this deployment, and it had nothing to do with how fast
inference is. On a GPU host where a question takes 10s instead of 400s, the
ceiling would still be 15 — requests would just churn through it faster.

Worth noting the ceiling bound at **ten** users rather than fifteen. Once some
requests fail instantly, a load generator with no think time immediately fires
replacements, and those compete for the pool with the slow requests still holding
connections. The practical limit is below the arithmetic one.

### 2. Pool exhaustion was an unhandled 500 — **fixed**

`db/base.py` translates connection failures to `DatabaseUnavailable`, which
`api/main.py` maps to a 503 with a useful hint. It caught
`OperationalError, InterfaceError, OSError`.

A pool timeout raises `sqlalchemy.exc.TimeoutError`, whose MRO is
`TimeoutError → SQLAlchemyError → Exception`. It is not a subclass of any of
those three, so it was not translated, and the caller got an opaque 500 after
waiting 30 seconds. Verified statically before the run and confirmed by it.

Now translated to `ConnectionPoolExhausted` — a subclass of `DatabaseUnavailable`,
so the existing handler catches it — with its own message, because the diagnosis
is the opposite of its parent's: Postgres is healthy and this process is holding
every connection it may open. The generic hint ("Is Postgres running?") would
send the reader to the wrong machine.

```
503  Retry-After: 5
{
  "detail": "The system is busy. Please retry in a few seconds.",
  "hint": "All 15 database connections are in use. Each in-flight question
           holds one for the whole agent run, so this is the concurrency
           ceiling, not a database fault."
}
```

Covered by `tests/test_pool_exhaustion.py`, which exhausts a real pool rather
than mocking the exception — the bug was about *which* exception SQLAlchemy
raises, so a hand-raised one would have passed against the broken code. Verified
to fail without the fix: 5 of its 10 tests do.

**This makes the failure honest; it does not raise the ceiling.** The real fix is
not holding a connection across an inference call, which is item 1 below and
still open.

### 3. The 300s inference read timeout is tuned for one user

`INFERENCE_READ_TIMEOUT=300` was chosen for a CPU host generating at 2.8–4.2
tok/s, where a slow answer genuinely takes minutes. Under queueing it stops
distinguishing "slow" from "stuck": a request that waits 300s for a slot is
indistinguishable from one whose model died, and both surface as
`inference_error`.

## What this does not tell you

- **Not a verdict on the cloud deployment.** This is one CPU laptop also running
  a browser, an IDE, and Postgres. The GPU host the compose file targets would
  change per-request latency by roughly an order of magnitude — but note that it
  would *not* change findings 1 and 2, which are properties of the application.
- **Not a capacity number for the containerized stack.** Docker is not installed
  here, so this ran against the host processes.
- **Thin at the top end.** Ten minutes per level and turns measured in minutes
  means single-digit completions at every level above one. The failure counts are
  large and solid; the latency percentiles at 5 and 10 users rest on four samples
  each and should be read as indicative.
- **Saturation, not a realistic arrival pattern.** No think time, all users
  arriving simultaneously. Real traffic is smoother, and the queue would be
  correspondingly shorter.

## If the goal were to serve 20 users

In rough order of payoff:

1. ~~**Release the DB session before the inference call and re-acquire to write
   the turn.**~~ — done, and measured: see the follow-up section above. Pool
   exhaustion no longer occurs at 10 or 20 users.
2. ~~**Translate pool exhaustion to a 503**~~ — done, see finding 2.
3. **A batching inference server.** vLLM's continuous batching is the answer to
   flat throughput, and `docker-compose.vllm.yml` already exists — the numbers at
   the top of this file are the argument for it.
4. **A request queue with an admission limit**, so load sheds visibly at the door
   rather than by timing out four minutes in.
5. **Lower `INFERENCE_READ_TIMEOUT`** once generation is fast, so a stuck call
   fails in a useful amount of time.

## Reproducing

```bash
python benchmarks/provision_users.py --users 20
DURATION=10m LEVELS="1 5 10 20" bash benchmarks/run_sweep.sh
python benchmarks/summarize_sweep.py
```

`benchmarks/README.md` documents the three harness bugs this test had before it
produced trustworthy numbers — each of which yielded a plausible-looking wrong
answer first.
