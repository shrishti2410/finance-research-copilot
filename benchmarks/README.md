# benchmarks/

Measurements that inform the project without being part of it. Nothing here is
imported by the app, runs in CI, or changes a default. It is deliberately outside
the deployment: the backend image's `COPY` list does not include this directory,
so none of it ships.

## `colab_t4_decode_benchmark.ipynb`

Upload to [Google Colab](https://colab.research.google.com), set **Runtime →
Change runtime type → T4 GPU**, and run all cells. Free tier; about five minutes,
most of it downloading the model.

It runs the hand-written greedy decode loop from milestone 1
(`llm-internals/transformer_walkthrough.py`, which lives beside this repo rather
than in it) on Colab's GPU, then on Colab's CPU, and compares both against the
numbers measured on the dev host.

The question it answers: **how much of this project's latency is the absence of a
GPU?** Everything about the app's inference configuration — the 1.5b/7b router
split, the 512-token generation cap, accepting +25% end-to-end to raise `num_ctx`
for correctness — follows from `docs/INFERENCE.md`'s finding that generation is
~97% of request latency at 2.8–4.2 tok/s. This puts a number on how much of that
is the hardware.

### Two things the port had to change, and why

A naive copy of the CPU loop onto a GPU reports nonsense. Both fixes are in the
notebook with the reasoning inline:

- **`torch.cuda.synchronize()` around every timed step.** CUDA kernels are
  asynchronous, so timing them the way the CPU version does measures how fast
  Python can *queue* work. Without this you get thousands of tokens/sec and a
  fiction.
- **`float16`, not `bfloat16`.** The original picks bf16 on CUDA, which is right
  for Ampere and later. A T4 is Turing (7.5) with no bf16 tensor cores, so torch
  accepts it and emulates it slowly. The notebook reads the compute capability
  and picks accordingly rather than assuming.

There is also a warm-up pass excluded from all timings, because the first CUDA
forward pass pays for context creation and kernel autotuning and would otherwise
land entirely on step 0.

### Reading the result

The honest comparison is GPU vs CPU **on the same machine**, which the notebook
measures directly. Comparing its GPU number against the app's 7B Ollama figures
moves two variables at once — model size and quantized llama.cpp vs unquantized
transformers — so that ratio is a ceiling on what a GPU would buy the product,
not an estimate of it. Batch size is 1 throughout, which leaves a GPU almost
idle; that understates what a batching server like vLLM gets from the same card.

Section 8 of the notebook states the caveats in full.

---

## `locustfile.py` — the full stack under concurrent load

```bash
# Any run that will be compared against another needs FRESH=1.
FRESH=1 DURATION=10m LEVELS="1 5 10 20" bash benchmarks/run_sweep.sh
python benchmarks/summarize_sweep.py
```

**`FRESH=1` is not optional for a comparison.** Reusing a pool does not reset the
conversations, and `AGENT_HISTORY_MESSAGES=10` replays a window of prior turns, so
a pool that has already been swept sends a much larger prompt on every request.
Re-running the sweep on a reused pool put 14 of 20 conversations at or past that
window (one held 62 messages) and read **250s** single-user median against the
**88s** the same code measured on a fresh pool. Nothing about the system had
changed; the harness had aged.

Milestone 2's [`scripts/load_test.py`](../scripts/load_test.py) measured the
inference proxy alone. This measures what a user waits for: one `/ask/stream` is
several inference calls plus tool execution plus two database writes, which on a
CPU host is minutes rather than seconds.

### What it measures, and what it cannot

The inference server barely batches. Measured directly with the M2 harness on
this host:

| concurrency | system tok/s | per-stream tok/s | scaling |
|---|---|---|---|
| 1 | 20.2 | 20.2 | 1.00x |
| 2 | 21.4 | 16.1 | 1.06x |
| 4 | 23.1 | 12.6 | 1.15x |

4x the concurrency buys 15% more throughput. So the full-stack sweep is mostly a
measurement of **queueing**, and flat throughput is the expected result rather
than a defect. What it can still answer precisely: where the queue forms, how
latency and time-to-first-token diverge as it grows, and at what concurrency the
stack starts failing rather than merely slowing.

### Five bugs this harness had, all found by running it

Kept here because each produced a plausible-looking wrong answer, which is the
failure mode a load test is most prone to.

**1. Locust does not time a streamed response.** With `stream=True` the built-in
timer stops when the response *headers* arrive. At concurrency 1 it reported
**31 ms** for turns that actually took over three minutes. Total latency and TTFT
are now fired by hand via `events.request.fire`, and the built-in entry is renamed
`POST /ask/stream (to headers only)` — it is still worth having, because it is the
auth and ownership check that happens before the agent starts.

**2. Guard outcomes are not failures.** The first smoke run reported a 50%
failure rate. The "failure" was `stop_reason=unverified_figures` — the grounding
guard refusing to ship figures it could not trace to a tool result, which is the
behaviour this project exists to have. Only `inference_error` and a missing
terminal frame count as failures now; every other `stop_reason` is recorded by
name, because a *rise* in guard activations under load would itself be a load
effect worth seeing.

**3. Tokens expire in 30 minutes; the sweep runs longer.** This one invalidated a
whole hour-long run. `JWT_EXPIRE_MINUTES=30`, tokens expired mid-sweep, and an
expired token does not fail cleanly: the rate limiter can no longer identify a
user, so it falls back to the **anonymous per-IP bucket of 20/min**, and a harness
with no think time becomes a 429 generator — roughly 70,000 of them in five
minutes, while the latency table still looked reasonable. Fixed three ways:
`provision_users.py --refresh` re-logs-in the pool, `run_sweep.sh` calls it before
every level, and the locustfile now aborts the user on any 401 or 429 rather than
looping.

**4. A reused account pool ages.** See `FRESH=1` above: the pool's conversations
keep their history between sweeps, and the replayed window makes every later run
slower for reasons outside the code under test. Caught by noticing that a
single-user median had tripled after a change that only touched connection
lifetimes — which could not plausibly have slowed generation down.

**5. Locust's request rate counts timeouts as throughput.** `Requests/s` counts
every *finished* run, and an `inference_error` finishes. So under saturation the
rate keeps rising while users receive nothing. The first post-fix sweep reported
**7.95x** throughput at 20 users when 14 of its 16 runs had timed out; the rate
of runs that actually delivered a response was **0.99x**. `summarize_sweep.py`
now prints `runs/min` and `ok/min` side by side and scales on the delivered rate.
Caught because 8x throughput from a server measured at 1.15x batching is not a
result, it is a unit error.

### Keeping runs apart

`OUT=benchmarks/results/<name> bash benchmarks/run_sweep.sh` writes a sweep's CSVs
to their own directory, and `python benchmarks/summarize_sweep.py <dir>` reads
them back. Without that, a second configuration overwrites the first one's CSVs.

---

## `replica_ab.py` and `router_overhead.py` — replicas behind a router

```bash
python benchmarks/replica_ab.py                  # ~1 h: 6 configurations x 2 rounds
python benchmarks/router_overhead.py synthetic   # ~16 min: the router alone, fake upstream
python benchmarks/router_overhead.py ollama      # ~20 min: direct vs router, interleaved
```

These measure the inference servers alone, not the agent, using the same request
loop as `scripts/load_test.py`:
- **`replica_ab.py`** compares one Ollama, two behind `inference_router`, two with
  the thread budget split, and one at `NUM_PARALLEL=2`.
- **`router_overhead.py`** takes apart whether the router costs throughput, and if
  so through which mechanism.

Results, and the recommendation that follows from them, are in
[`docs/INFERENCE_REPLICAS.md`](../docs/INFERENCE_REPLICAS.md). All of it is measured
on `qwen2.5:1.5b`, because two 7b replicas do not fit in this machine's RAM.

### Why accounts are pre-provisioned

`/auth/*` is limited to 10 requests per minute **per IP regardless of token**,
because signup and login are the brute-force surface. Every simulated user is
127.0.0.1, so signing up inside the test would measure the rate limiter instead of
the agent. `provision_users.py` creates the pool beforehand, paced under that
limit, and writes `results/users.json` — which is gitignored, because it holds
real bearer tokens.
