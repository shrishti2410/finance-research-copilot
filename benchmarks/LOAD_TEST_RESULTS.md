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

### 1. A database connection is held for the entire agent run

`/ask` and `/ask/stream` both take `session: AsyncSession = Depends(get_session)`.
FastAPI holds a dependency for the whole request, and for a stream that is the
whole response — minutes. So one Postgres connection is pinned per in-flight
question, for its full duration, while the agent is doing nothing but waiting on
the model.

`db_pool_size=5` + `db_max_overflow=10` = **15 connections**. That is the hard
concurrency ceiling of this deployment, and it has nothing to do with how fast
inference is. On a GPU host where a question takes 10s instead of 400s, the
ceiling would still be 15 — requests would just churn through it faster.

Worth noting the ceiling bound at **ten** users rather than fifteen. Once some
requests fail instantly, a load generator with no think time immediately fires
replacements, and those compete for the pool with the slow requests still holding
connections. The practical limit is below the arithmetic one.

### 2. Pool exhaustion is an unhandled 500, not a 503

`db/base.py` translates connection failures to `DatabaseUnavailable`, which
`api/main.py` maps to a 503 with a useful hint. It catches
`OperationalError, InterfaceError, OSError`.

A pool timeout raises `sqlalchemy.exc.TimeoutError`, whose MRO is
`TimeoutError → SQLAlchemyError → Exception`. It is not a subclass of any of
those three, so it is not translated, and the caller gets an opaque 500 after
waiting 30 seconds. Verified statically before the run and confirmed by it.

The one-line fix is to add `sqlalchemy.exc.TimeoutError` to that tuple. The real
fix is not holding a connection across an inference call.

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

1. **Release the DB session before the inference call and re-acquire to write the
   turn.** Removes the 15-connection ceiling, which currently binds before
   anything else.
2. **Translate pool exhaustion to a 503** so an overloaded deployment says so.
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
