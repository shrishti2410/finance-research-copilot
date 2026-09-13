# Inference replicas: a router in front of several inference servers

**Status:** architecture built, tested, and measured on this laptop with two CPU
Ollama instances. **Multi-GPU inference has not been run.** It needs hardware
this project does not have. See [what this does not demonstrate](#what-this-does-not-demonstrate).

## What was built

```
                     ┌──────────────► Ollama replica 1  :11434  (full copy of the model)
 app ──► /v1 proxy ──► inference_router :11400
         (api/)      └──────────────► Ollama replica 2  :11435  (full copy of the model)
```

[`inference_router/`](../inference_router/app.py) is a small HTTP reverse proxy.
Each request goes to the next replica in strict rotation. The app does not
change: `INFERENCE_BASE_URL` is the only setting that moves, from one replica's
address to the router's.

```bash
# second replica, same settings as the first
OLLAMA_HOST=127.0.0.1:11435 OLLAMA_NUM_PARALLEL=1 OLLAMA_CONTEXT_LENGTH=8192 ollama serve

python -m inference_router --port 11400 \
    --upstream http://127.0.0.1:11434 --upstream http://127.0.0.1:11435

INFERENCE_BASE_URL=http://127.0.0.1:11400/v1 uvicorn api.main:app --port 8000
curl http://127.0.0.1:11400/router/stats      # per-replica counts
```

Not to be confused with `AGENT_ROUTER_MODEL`, which sends the *steps* of one
agent run to a small or a large model. This router sends whole *requests* to
identical replicas, and knows nothing about models.

### Design decisions, and why

| Decision | Reason |
|---|---|
| Separate process, not a URL list inside `api/inference_proxy.py` | A real deployment has this shape: a load balancer in front of inference nodes. The app stays unchanged. |
| Forwards any path and relays raw bytes | `/v1/*` and Ollama's `/api/*` both work. Streams are never buffered, which is the same rule the app's proxy follows. |
| Retries only when the connection is refused or times out | Those requests never reached a replica. Any request that did may already have generated, and resending it could run it twice. So a 500 or a read timeout from a replica goes back to the caller. |
| Read timeout 900s, longer than the app's 300s | The caller decides how long to wait. If the router timed out first, the app's clean `inference_error` would become a 504 from a hop it doesn't know exists. |
| Unbounded connection pool | If the router queued requests in its own pool, it would hide where the wait really is. Queueing belongs to the replicas, where a load test can see it. |
| Counts `busy_while_idle` | Round-robin ignores load. This counts generations sent to a replica that was already busy while another sat idle, so the cost of that blindness is measured, not guessed. |
| No prompt-cache affinity | Consecutive calls from one agent run alternate between replicas, so each call loses the previous replica's cached prompt prefix. On CPU prefill is expensive, so this is a real cost of plain round-robin. It's listed under limits below. |

Tests: [`tests/test_inference_router.py`](../tests/test_inference_router.py) covers
rotation, byte-for-byte streaming, pass-through of error statuses, failover
only on connection failure, the 503 when every replica is down, and the
accounting.

## What this does not demonstrate

There are two different things called distributed inference. This project can
show one of them.

**Data parallelism (replicas): demonstrated here.** Every replica holds a
complete copy of the model, and a request runs start to finish on one replica.
Replicas raise how many requests can run at once. They do nothing for how fast
one request runs, or for a model too large to fit on one device. The router
above is the whole mechanism, and it is identical whether the replicas are CPU
processes or GPU nodes.

**Model parallelism (tensor or pipeline): not demonstrated, and not
demonstrable here.**
- *Tensor parallelism* splits one model's weight matrices across several GPUs, so
  every layer needs an all-reduce between them. It needs GPUs joined by a fast
  interconnect (NVLink, or PCIe at minimum).
- *Pipeline parallelism* puts different layers on different devices.

Both exist to serve a model larger than one device's memory, or to cut
per-token latency. vLLM does this with `--tensor-parallel-size N`. That flag
needs N CUDA GPUs, and this machine has none (Intel Iris Xe, no CUDA, no WSL;
see `docs/DEPLOYMENT.md`). No router, proxy, or second CPU process can stand in
for it: the problem is splitting one model's computation, not distributing
requests.

So "multi-GPU distributed inference" in this project means **written and
reasoned about, not run**. That is the same status, and the same reason (cost),
as `docker-compose.gpu.yml`.

## Measured on this laptop

### Which model each number comes from

**The replica comparison used `qwen2.5:1.5b`, not the app's production
`qwen2.5:7b` model. Two 7b replicas cannot fit in this machine's 15.7 GB of RAM
alongside the OS and the other running services.** Measured, not estimated:

- Loaded at 8192 context, 7b holds **5,318 MB** of private memory and 1.5b holds
  **1,412 MB**. `ollama ps` prints these in decimal units, as 5.5 GB and
  1.4 GB. With the app's normal pair loaded (7b + 1.5b), **1.05 GB** of RAM was
  free.
- Replicas cannot share weights. Ollama logs `disabling mmap for llama-server
  load by default ... reason=cpu`, so each runner reads the model into private
  memory. A second 7b replica would need another 5.3 GB on top of the first.
- With one 1.5b copy on each replica and nothing else loaded, 4.0 GB stayed
  free.

**What was measured at 7b, and what was not:**

- **Two 7b replicas: not run in any form.** They cannot be loaded at the same
  time, so neither their throughput nor their combined memory was measured.
  Wherever this document gives "+5,318 MB" for a second 7b replica, that is one
  7b runner's measured private memory. It stands for what a second, unshared
  copy would need; it is not a reading of two loaded together.
- **One 7b replica at `NUM_PARALLEL=1` vs `NUM_PARALLEL=2`: memory and
  throughput both measured.** That configuration fits (5,768 MB), so it was
  measured instead of assumed. See the 7b table under the recommendation.

Every throughput figure not labelled 7b is from 1.5b.

### Setup

- **Hardware:** Intel i5-1235U (2 performance cores with hyper-threading + 8
  efficiency cores: 10 cores, 12 threads), 15.7 GB RAM.
- **Ollama:** 0.34.0. Every replica runs `OLLAMA_NUM_PARALLEL=1`,
  `OLLAMA_CONTEXT_LENGTH=8192`, and llama-server's default 6 threads unless a
  configuration says otherwise.
- **Requests:** `scripts/load_test.py`'s request loop, the same one milestone 2
  used, so an inference call means the same thing in both measurements. 128 max
  tokens, distinct prompts, temperature 0.
- **Levels:** 1, 2, 4 and 8 concurrent, sending 6, 8, 8 and 16 requests
  respectively.
- **Rounds:** two, the second in reverse order so thermal drift books to both
  ends. Each configuration starts from nothing loaded, with models warmed on
  each replica directly.
- **Result:** 456 requests, **0 failures**.

```bash
python benchmarks/replica_ab.py            # ~1 hour; writes benchmarks/results/replicas/
```

System throughput in tok/s, mean of the two rounds with [min–max]:

| configuration | c=1 | c=2 | c=4 | c=8 |
|---|---|---|---|---|
| direct, 1 replica | 20.5 [19.5–21.5] | 20.2 [19.9–20.4] | 21.0 [20.7–21.2] | 19.4 [18.3–20.5] |
| router, 1 replica | 23.9 [23.2–24.6] | 20.0 [19.4–20.6] | 19.6 [19.2–20.0] | 19.1 [18.4–19.8] |
| router, 2 replicas | 22.3 [21.7–23.0] | 20.4 [18.4–22.3] | 20.0 [17.9–22.1] | 21.1 [19.9–22.2] |
| router, 2 replicas, 3 threads each | 16.2 [15.8–16.6] | 23.1 [22.4–23.9] | 23.0 [23.0–23.0] | 20.5 [20.1–20.9] |
| router, 1 replica, 3 threads | 16.3 [16.2–16.5] | 16.9 [16.7–17.1] | 17.3 [17.0–17.6] | 16.5 [16.5–16.6] |
| router, 1 replica, NUM_PARALLEL=2 | 23.4 [22.5–24.2] | **26.8** [26.6–27.1] | **25.3** [23.8–26.8] | **25.3** [25.3–25.3] |

Relative to "router, 1 replica" at the same concurrency. "Peak" compares each
configuration's best level with the baseline's best level.

| configuration | c=1 | c=2 | c=4 | c=8 | peak vs peak |
|---|---|---|---|---|---|
| direct, 1 replica | 0.86x | 1.01x | 1.07x | 1.01x | 0.88x |
| router, 2 replicas | 0.93x | 1.02x | 1.02x | 1.10x | 0.93x |
| router, 2 replicas, 3 threads each | 0.68x | 1.16x | 1.17x | 1.07x | 0.97x |
| router, 1 replica, 3 threads | 0.68x | 0.84x | 0.88x | 0.86x | 0.72x |
| router, 1 replica, NUM_PARALLEL=2 | 0.98x | **1.34x** | **1.29x** | **1.32x** | **1.12x** |

Median time to first token (s) / per-stream tok/s. Per-stream tok/s includes
time spent queueing:

| configuration | c=1 | c=2 | c=4 | c=8 |
|---|---|---|---|---|
| direct, 1 replica | 0.35 / 21.0 | 6.44 / 11.5 | 17.03 / 9.0 | 45.12 / 5.0 |
| router, 1 replica | 0.29 / 24.1 | 6.21 / 11.8 | 18.23 / 8.4 | 45.16 / 4.8 |
| router, 2 replicas | 0.34 / 22.6 | 0.17 / 10.4 | 12.59 / 6.5 | 33.87 / 4.3 |
| router, 2 replicas, 3 threads each | 0.42 / 16.2 | 0.14 / 12.0 | 10.76 / 7.6 | 33.52 / 4.3 |
| router, 1 replica, 3 threads | 0.41 / 16.3 | 7.63 / 9.6 | 21.07 / 6.9 | 51.83 / 4.1 |
| router, 1 replica, NUM_PARALLEL=2 | 0.29 / 23.4 | 0.15 / 13.8 | 9.34 / 8.6 | 28.08 / 5.1 |

Router accounting: every 2-replica run split its requests exactly **19 / 19**,
and `busy_while_idle` was **0** in all ten router runs. Read that zero as a
property of this test, not of round-robin. The requests are all the same length
and arrive in lockstep, so strict rotation is already optimal. Agent traffic
mixes 35-token routing steps with 512-token answers, and there this count is
expected to be non-zero. It was not measured under agent traffic.

### What it says

**A second replica does not measurably help on this CPU.** Its best result is
1.10x, at c=8. That is smaller than the spread between its own two rounds: at
c=2 it read 22.3 in one round and 18.4 in the other. Peak against peak it is
0.93x. The expectation going in was "a small win at best", and that is an
overstatement: the win cannot be told apart from zero. Two reasons fit the
hardware:
- **Memory bandwidth, not cores, limits decoding.** Every generated token reads
  the whole model's weights, and two processes share one memory bus.
- **Two default replicas oversubscribe the CPU.** They ask for 12 compute threads
  on 10 cores, 8 of which are efficiency cores.

**Splitting threads helps under load and costs a lone user.** Two replicas at 3
threads each hold the thread budget at one replica's 6, and they reach
1.16–1.17x at c=2 and c=4. But a request running on its own now gets only 3
threads, so c=1 falls to 0.68x. By c=8 the gain has shrunk to 1.07x.

**Batching inside one replica wins.** `NUM_PARALLEL=2` gave 1.29–1.34x at every
concurrency from 2 up, 0.98x for a lone request, and the lowest time to first
token at every loaded level (28s against 45s at c=8). It was also the most
repeatable configuration: 25.3 tok/s at c=8 in both rounds. Batching lets two
sequences share each pass over the weights. Two processes cannot share that
work, and on a bandwidth-bound machine that is the difference that matters.

### Recommendation for more concurrent users on CPU: NUM_PARALLEL=2, not a second replica

Memory for each option, measured by loading each configuration on its own and
reading llama-server's private memory:

| model | NUM_PARALLEL=1 | NUM_PARALLEL=2 | extra for NUM_PARALLEL=2 | extra for a second replica |
|---|---|---|---|---|
| qwen2.5:1.5b | 1,412 MB | 1,636 MB | **+224 MB** | +1,412 MB |
| qwen2.5:7b | 5,318 MB | 5,768 MB | **+450 MB** | +5,318 MB (does not fit here) |

`NUM_PARALLEL=2` does not split one context in half. Ollama launched llama-server
with `-c 16384 -np 2`, which gives each slot the same 8192 tokens it had before.
The extra memory is only the second slot's KV cache. The weights exist once,
because it is still one process.

Putting memory and throughput together, for 1.5b:

| option | extra memory | throughput vs 1 replica at c=2 / 4 / 8 | a single request |
|---|---|---|---|
| second replica | +1,412 MB | 1.02x / 1.02x / 1.10x (inside noise) | 0.93x |
| two replicas, 3 threads each | +1,412 MB | 1.16x / 1.17x / 1.07x | 0.68x |
| **NUM_PARALLEL=2** | **+224 MB** | **1.34x / 1.29x / 1.32x** | 0.98x |

**The trade-off:** `NUM_PARALLEL=2` costs about a sixth of a second replica's
memory. For that it buys roughly three times the throughput gain (1.32x against
1.10x at c=8). Median time to first token at c=8 drops from 45s to 28s, and a
lone request gives up nothing measurable.

**At 7b, the production model, most of the throughput gain disappears.** One
replica of qwen2.5:7b, 128 max tokens, 3/4/8 requests at c=1/2/4, two rounds in
ABBA order, 0 failures in 60 requests (`replica_ab.py --model qwen2.5:7b`):

| qwen2.5:7b | c=1 | c=2 | c=4 |
|---|---|---|---|
| NUM_PARALLEL=1, tok/s [min–max] | 5.0 [5.0–5.0] | 5.2 [5.0–5.3] | 5.0 [4.6–5.4] |
| NUM_PARALLEL=2, tok/s [min–max] | 5.3 [5.2–5.4] | 5.6 [5.6–5.6] | 5.2 [4.8–5.6] |
| throughput ratio | 1.05x | 1.08x | 1.03x |
| median TTFT, NUM_PARALLEL=1 → 2 | 1.50 → 1.44 s | **19.24 → 0.45 s** | **70.4 → 48.1 s** |

The gain is 1.03–1.08x, and at c=4 the two settings' ranges overlap. Compare
1.29–1.34x at 1.5b. One possible reason, which was not measured: at 7b each token
is compute-bound as well as bandwidth-bound, so a second sequence in the batch
costs real compute instead of riding along on weight reads that were happening
anyway.

What survives is **latency**. With two requests at once, the second no longer
waits for the first to finish: its time to first token falls from 19 s to under
half a second. At c=4 the median wait falls from 70 s to 48 s. At the production
model, then, `NUM_PARALLEL=2` is a latency setting more than a throughput
setting.

| trade-off | extra memory | throughput | time to first token under load |
|---|---|---|---|
| NUM_PARALLEL=2 at 1.5b | +224 MB | 1.29–1.34x | 45 s → 28 s at c=8 |
| NUM_PARALLEL=2 at 7b | +450 MB | 1.03–1.08x | 19 s → 0.45 s at c=2, 70 s → 48 s at c=4 |
| second replica at 1.5b | +1,412 MB | ≤1.10x, inside noise | 45 s → 34 s at c=8 |
| second replica at 7b | +5,318 MB | not measurable: does not fit | – |

The alternatives lose for different reasons:
- **A second replica** pays for a full copy of the weights and gets nothing back
  that can be measured on this CPU.
- **Thread-splitting** is the better of the two replica variants, but it takes
  single-user speed down to 0.68x to buy its gain.

Limits of this recommendation:

- **The throughput case is a 1.5b result.** At 7b, the model that writes the app's
  answers, `NUM_PARALLEL=2` measured 1.03–1.08x: a latency improvement, not a
  capacity one. Two 7b replicas were tested for neither throughput nor combined
  memory, because they do not fit.
- **The app's normal pair gets tight.** Running 7b and 1.5b together, both at
  `NUM_PARALLEL=2`, adds about 674 MB. That leaves roughly 0.4 GB of the 1.05 GB
  that was free on this laptop. It fits, with little margin. On a GPU VM it would
  not matter.
- **It has not been measured through the agent.** 1.3x is a queue that moves
  about 30% faster. It can raise the full-stack ceiling in
  `benchmarks/LOAD_TEST_RESULTS.md` (between 5 and 10 users) by at most that
  factor.
- **Only 1 and 2 were tested.** `NUM_PARALLEL` 3 or higher was not.
- **Not applied.** `OLLAMA_NUM_PARALLEL` stays 1 in `docker-compose.yml` and in the
  local setup. Changing it means accepting the memory trade-off above, and that is
  a decision rather than a default.

The router still earns its place, for everything except throughput on one CPU. It
is the component that turns "one replica per GPU" into a single endpoint. It
fails over when a replica refuses connections. And it counts where round-robin's
blindness costs a queue.

### The router's own cost

In the replica runs, "1 replica via router" and "1 replica direct" differ in both
directions:
- **c=1:** the router run was *faster* in both rounds (23.2 and 24.6 vs 19.5 and
  21.5). A proxy cannot make generation faster, so a gap that size is noise.
- **c=4:** the router was slower in both rounds, by 5–7%.
- **c=2 and c=8:** it went one way in round 1 and the other in round 2.

Two rounds cannot separate a real cost from noise. So
[`benchmarks/router_overhead.py`](../benchmarks/router_overhead.py) measures the
fingerprint each possible cause would leave, alternating direct and router runs
ABBA four times.

**1. Against a fake upstream (no model), the router alone.** 16 requests of 128
tokens, mean ± sd over 4 alternations:

| upstream pace | c | path | system tok/s | TTFT p50 | router CPU per token |
|---|---|---|---|---|---|
| 50 ms/token (CPU-model pace) | 1 | direct | 17.97 ±0.04 | 60 ms | – |
| | 1 | router | 17.92 ±0.03 | 62 ms | 816 µs |
| | 8 | direct | 135.5 ±2.6 | 64 ms | – |
| | 8 | router | 132.3 ±1.5 | 90 ms | 416 µs |
| flat out | 1 | direct | 15,417 ±459 | 3 ms | – |
| | 1 | router | 11,933 ±847 | 5 ms | 55 µs |
| | 8 | direct | 10,672 ±4,470 | 25 ms | – |
| | 8 | router | 8,640 ±3,707 | 65 ms | 111 µs |

What this rules in and out:

- **Extra hop latency: real, and too small to matter.** 2 ms on TTFT with one
  request, 26 ms with eight. Against a TTFT of seconds to tens of seconds on the
  real model, that disappears.
- **Relay cost per chunk: real, and paid per wake-up, not per byte.** Relaying one
  small SSE frame at a time costs 0.4–0.8 ms of router CPU per token. Frames
  that arrive together (flat out) cost 55–111 µs per token. At Ollama's ~20
  tok/s that is about 1.6% of one core.
- **At CPU generation rates the router costs no throughput:** −0.3% at c=1, and
  −2.4% at c=8, which is inside direct's own ±2.6 spread.
- **The relay has a ceiling, around 10,000 tok/s.** Flat out, it cut throughput
  by roughly 20%. That is about 500x what this CPU generates, so it is irrelevant
  here. A batched GPU server can reach it: this is a Python process relaying
  every token, and a production deployment would put a compiled proxy (nginx,
  Envoy) in this role.
- **Connection pooling: ruled out by configuration.** The router's pool is
  unbounded, and the harness allows concurrency + 10 connections.

**2. Against real Ollama, direct vs router interleaved.** One replica of
`qwen2.5:1.5b` at `NUM_PARALLEL=1`, 128 tokens per request, mean ± sd over 4
alternations:

| c | path | system tok/s | decode tok/s within a stream | TTFT p50 | time not streaming | router CPU per token |
|---|---|---|---|---|---|---|
| 1 | direct | 21.98 ±0.94 | 21.94 ±0.97 | 0.072 s | 0.52 ±0.13 s | – |
| 1 | router | 21.46 ±0.69 | 21.55 ±0.66 | 0.077 s | 0.49 ±0.02 s | 999 µs |
| 8 | direct | 20.25 ±1.47 | 20.76 ±1.41 | 41.6 s | 1.32 ±0.10 s | – |
| 8 | router | 20.78 ±0.38 | 21.20 ±0.40 | 40.2 s | 1.32 ±0.04 s | 940 µs |

**Verdict: there is no consistent throughput cost.** At c=8 the router run was
2.6% *faster* on the mean. At c=1 it was 2.3% slower. Both differences are
smaller than the direct path's own standard deviation. Each candidate cause
leaves a fingerprint, and none of them shows up:

- **CPU contention: absent.** If the router's ~1 ms of CPU per token were taking
  time from llama-server, generation inside each stream would slow down. It did
  not: 21.2 tok/s via the router and 20.8 direct at c=8.
- **Handoff gaps: absent.** With one generation slot, a relay that held a
  finished stream open would delay the next request's start. Time not streaming,
  meaning prefill plus gaps across all 16 requests, was 1.319 s via the router and
  1.316 s direct.
- **Hop latency: present, at 5 ms of TTFT.**

So the 5–7% router deficit at c=4 in the replica runs was noise, not a cost.
Two samples per cell cannot resolve a 5% difference on this laptop: across four
alternations here, direct alone ranged from 18.2 to 21.6 tok/s at c=8, which is
wider than the gap being explained.

One caveat. The router spends about 1 ms of CPU per token against real Ollama,
roughly ten times its flat-out cost per token, because every token arrives as
its own wake-up. At 20 tok/s that is invisible. The same per-wake-up cost is
what puts the ceiling of about 10,000 tok/s in part 1 above.

```bash
python benchmarks/router_overhead.py synthetic    # ~16 min
python benchmarks/router_overhead.py ollama       # ~20 min
```

## What changes on real GPU hardware (not run)

The architecture carries over unchanged. What changes is the payoff.

- **One replica per GPU.** Each GPU has its own memory and its own memory
  bandwidth, which is what decoding is limited by. On CPU, two processes share
  one memory bus. With one replica per GPU, throughput should scale nearly
  linearly with the number of GPUs. That is an expectation from how the
  hardware works, not a measurement.
- **Each replica batches.** vLLM's continuous batching serves many sequences per
  GPU at once, so on GPU the replica count multiplies an already-batched
  throughput. Ollama on this CPU barely batches (1.15x at 4x concurrency).
- **Pin each replica to its GPU.** In compose: one `ollama` (or `vllm`)
  service per GPU, each reserving `device_ids: ["0"]`, `["1"]`, and so on,
  instead of `count: all`. Then run the router as one more service and point
  `INFERENCE_BASE_URL` at `http://inference-router:11400/v1`.
- **Round-robin stops being good enough.** It ignores request length, and agent
  calls range from a 35-token routing step to a 512-token answer. A production
  router would send each request to the replica with the fewest outstanding
  requests, and keep each conversation on one replica so its prompt prefix stays
  in that replica's cache. The `busy_while_idle` counter is the number that shows
  when this becomes worth building.
- **At that point, use an off-the-shelf router.** Nginx, Envoy, and LiteLLM all do
  the job. This one exists to show the architecture and to be measured, not to
  run in production.
