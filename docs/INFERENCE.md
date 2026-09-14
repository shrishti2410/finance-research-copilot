# Inference setup

The app never talks to a model directly. It talks to an **OpenAI-compatible
server** at `INFERENCE_BASE_URL`, proxied through
[`api/inference_proxy.py`](../api/inference_proxy.py).

That indirection is the whole point: vLLM, Ollama, and llama.cpp all expose
`/v1/chat/completions` with the same request and SSE response shape. Switching
between them is one environment variable, not a code change.

```
client ──▶ FastAPI :8000 ──▶ OpenAI-compatible server :8001 ──▶ model
           (this repo)        vLLM | Ollama | llama.cpp
```

---

## Read this first: vLLM will not run on Windows

vLLM ships **Linux-only** wheels. There is no native Windows build, and its CPU
backend must be compiled from source on Linux. On a Windows box you need WSL2 or
Docker, and for the CUDA build you need an NVIDIA GPU.

**This machine** (Intel Iris Xe integrated graphics, no WSL, 15.7 GB RAM) cannot
run vLLM in any configuration. Use Option B for local development; keep Option A
as the deployment target.

| | Option A — vLLM | Option B — Ollama (dev) |
|---|---|---|
| Platform | Linux / WSL2 / Docker | Native Windows, macOS, Linux |
| Hardware | NVIDIA GPU | CPU |
| Model | Qwen2.5-7B-Instruct | Qwen2.5-1.5B-Instruct |
| Throughput | Continuous batching, high | Single-stream, low |
| Use for | Staging, prod, evals | Writing code, wiring the proxy |

---

## Option A — vLLM with Qwen2.5-7B-Instruct (GPU)

### Sizing

Qwen2.5-7B at bfloat16 is **~15.2 GB of weights**, before the KV cache. Budget:

| GPU VRAM | Workable? | Settings |
|---|---|---|
| 24 GB (A10, 3090, 4090) | Comfortable | `--max-model-len 8192` |
| 16 GB (A4000, 4080) | Tight | `--max-model-len 4096 --gpu-memory-utilization 0.95` |
| < 16 GB | Not at bf16 | Use an AWQ/GPTQ 4-bit checkpoint, or drop to 1.5B |

If you are short on VRAM, prefer a quantized checkpoint over shrinking context:

```bash
vllm serve Qwen/Qwen2.5-7B-Instruct-AWQ --quantization awq --max-model-len 8192
```

### Native (Linux or WSL2 with NVIDIA drivers)

```bash
python -m venv .venv && source .venv/bin/activate
pip install vllm

vllm serve Qwen/Qwen2.5-7B-Instruct \
  --host 0.0.0.0 \
  --port 8001 \
  --served-model-name qwen2.5-7b-instruct \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.90
```

`--served-model-name` pins the string clients pass as `"model"`. Set it, or that
string becomes the full HuggingFace repo path and changes if you swap
checkpoints.

Startup takes a minute or two while weights load and the KV cache is profiled.
The port binds **before** the model is ready — which is exactly why
`/v1/health` probes `/v1/models` rather than just checking the socket.

### Docker (no local CUDA toolchain needed)

```bash
docker run --rm --gpus all -p 8001:8000 --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen2.5-7B-Instruct \
  --served-model-name qwen2.5-7b-instruct \
  --max-model-len 8192
```

`--ipc=host` matters: vLLM's workers use shared memory, and Docker's stingy
default `/dev/shm` causes confusing crashes under load.

Or use the committed compose file:

```bash
docker compose -f docker-compose.vllm.yml up
```

Then in `.env`:

```
INFERENCE_BASE_URL=http://127.0.0.1:8001/v1
INFERENCE_MODEL=qwen2.5-7b-instruct
```

---

## Option B — Ollama with Qwen2.5-1.5B-Instruct (CPU dev)

Runs natively on Windows, no GPU. Comfortable in ~2 GB of RAM at Q4.

```powershell
winget install Ollama.Ollama     # or download from ollama.com
ollama pull qwen2.5:1.5b-instruct
ollama serve                     # http://127.0.0.1:11434
```

Ollama exposes an OpenAI-compatible surface at `/v1`, so the proxy needs no
changes:

```
INFERENCE_BASE_URL=http://127.0.0.1:11434/v1
INFERENCE_MODEL=qwen2.5:1.5b-instruct
INFERENCE_API_KEY=ollama
```

Expect roughly 10-25 tokens/sec on CPU. Fine for wiring up plumbing and
eyeballing prompts; do not draw conclusions about answer quality from a 1.5B
model, and run `eval/` against Option A.

### Alternative: llama.cpp

If you want GGUF control or Intel GPU offload via Vulkan:

```bash
llama-server -hf Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M --port 8001
```

```
INFERENCE_BASE_URL=http://127.0.0.1:8001/v1
```

---

## Verifying

Start the upstream, then the app:

```bash
uvicorn api.main:app --reload --port 8000
```

**Readiness** — should report the upstream and the models it has loaded:

```bash
curl http://127.0.0.1:8000/v1/health
```

```json
{
  "status": "ok",
  "upstream": {"url": "http://127.0.0.1:11434/v1", "reachable": true,
               "latency_ms": 3.1, "models": ["qwen2.5:1.5b-instruct"]},
  "default_model": "qwen2.5:1.5b-instruct"
}
```

A `503` with `"status": "unavailable"` means the upstream is down; `"degraded"`
means it answered but not with a 200 (usually still loading weights).

**Buffered completion:**

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"What is a 10-K?"}]}'
```

**Streaming** — tokens should appear progressively, not all at once:

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Name three SEC filing types."}],"stream":true}'
```

`-N` disables curl's own buffering. Without it the stream looks broken even when
it is fine.

Or run the included script, which exercises all three:

```bash
python scripts/smoke_test.py
```

---

## Notes

- **`model` is optional.** The proxy fills in `INFERENCE_MODEL` when the client
  omits it, so client code doesn't hardcode checkpoint names.
- **SSE is relayed byte-for-byte.** The proxy never parses or re-serializes
  frames — that would add latency to every single token.
- **Client disconnects propagate.** If a caller hangs up mid-stream, the proxy
  closes the upstream response, releasing the vLLM scheduler slot instead of
  leaking it.
- **`X-Accel-Buffering: no`** is set on streamed responses. Deploying behind
  nginx without it means nginx buffers the whole stream and delivers it in one
  lump.

---

## Server settings on a CPU host, and what each one is worth

Measured on the dev host (Windows, 12 logical CPUs, 15.7 GB RAM, no GPU —
`size_vram: 0`), running `qwen2.5:7b` at Q4_K_M through Ollama 0.32.15. Every
number below is a median of three interleaved samples; the same configuration
measured 15.8 s and 23.1 s in different runs on this machine, so single samples
and block designs are both worthless here.

### Where the time actually goes

| step | prompt | prompt eval | output | generation | rate |
|---|---|---|---|---|---|
| tool selection | 1,730 tok | 0.3 s | 35 tok | 12.5 s | 2.8 tok/s |
| writing the answer | 1,919 tok | 0.3 s | 61 tok | 14.5 s | 4.2 tok/s |

**Generation is ~97% of latency.** Prompt evaluation is 0.3 s whether the
context window is 2048 or 8192. Anything that does not reduce generated tokens
or raise tokens/sec does not reduce latency.

Every configuration decision below follows from that one constraint, so it is
worth knowing how much of it is the hardware.
[`benchmarks/colab_t4_decode_benchmark.ipynb`](../benchmarks/colab_t4_decode_benchmark.ipynb)
answers that on a free Colab GPU, using the hand-written decode loop from
milestone 1 so the comparison is the same work rather than a different stack. On
this host that loop measures **2.67 tok/s** for `Qwen2.5-1.5B-Instruct` in
float32 (24 tokens in 8,995 ms, re-measured 2026-09-12) — the controlled baseline
the notebook compares against, distinct from the Q4_K_M llama.cpp numbers in the
table above.

### `OLLAMA_CONTEXT_LENGTH=8192`

Raised, not lowered, and for correctness rather than speed.

A smaller window is not faster *per step*: measured on one answer step, 2048 /
4096 / 8192 all landed inside a 17.5–23.1 s band with identical output. End to
end it does cost something — the same question ran 25.7 s at 4096 and 32.2 s at
8192 through the full loop, about +25%. That is the price, and it is worth
paying, because what 4096 *did* do was silently discard evidence. A 7,312-token conversation was evaluated as **3,970
tokens** at 4096 with no error of any kind. Markers placed at three positions
showed what goes: the system prompt survives, the **earliest tool results do
not**. That is the worst thing to lose in this agent — the answer step can arrive
with the figure it is supposed to cite no longer in context, which is exactly the
condition `agent/grounding.py` exists to catch after the fact.

Five of the 40 `eval/questions.json` cases have estimated final-iteration prompts
above 4096, the largest ~5,852 tokens. 8192 is the smallest power of two that
clears that plus the 512-token generation cap with margin — `num_ctx` is the
budget for prompt *and* output together, so 6144 would not have been enough for
the worst case.

The router split pays for this and more: 41.9 s at 4096 on 7b everywhere becomes
32.2 s at 8192 with 1.5b routing. The combination is both faster than the
original and no longer truncating.

The app calls the OpenAI-compatible `/v1` endpoint, which has **no `num_ctx`
field**, so this environment variable is the only way to set it. Check it took
effect with `curl -s localhost:11434/api/ps` and read `context_length` — and note
that on Windows the value must be in the environment of the process that starts
Ollama. Setting the user-scope variable and then launching the tray app from a
shell that predates the change leaves it at the old value.

### `OLLAMA_NUM_PARALLEL=1`

Ollama allocates the KV cache per parallel slot, so leaving this unset lets the
server pick a number and multiply the 8192-token cache by it. This workload is
strictly sequential — one agent loop, one iteration at a time — so extra slots
buy nothing and cost hundreds of megabytes on a host that had 0.64 GB free.

### `OLLAMA_MAX_LOADED_MODELS=2`

Required by the router split (`settings.agent_router_model`): the loop alternates
between `qwen2.5:7b` and `qwen2.5:1.5b` within a single turn. If the server
evicts one to load the other, each switch costs a **172–207 s** model load —
measured, and enough to make the split catastrophically worse than not having it.
Both runners stayed resident across 18 end-to-end runs, and at 8192 both
reported `context_length: 8192` simultaneously. Watch the memory though: free RAM
dipped to **0.08 GB** while both models were loading, on a 15.7 GB host. If that
becomes a problem before more RAM does, lower `agent_max_tokens` and drop
`OLLAMA_CONTEXT_LENGTH` to 6144 rather than going back to 4096.

### `max_tokens` on every request

Ollama's `num_predict` is unbounded by default, which on a CPU host is a
minutes-long tail rather than a longer answer. `settings.agent_max_tokens = 512`
clears the longest observed final answer (~368 tokens across the 40 eval cases;
median ~57) and binds only on a runaway. Lower caps do reduce latency, but only
by cutting the answer off mid-sentence: at 128 the model stopped at
`"…we can use the net income and revenue figures provided"`, with
`done_reason: length` and no figure. That is a missing answer, not a fast one.

### `INFERENCE_CALL_DEADLINE=900` — a cap on the call, not on the silence

`INFERENCE_READ_TIMEOUT=300` is an httpx read timeout: it bounds the gap between
bytes, not the length of a call. This was measured, not read from the docs. A fake
upstream on a real socket was driven through the agent's own call functions,
directly and through the real API proxy, with the read timeout shrunk to 2 s and a
12 s cap:

| upstream behaviour | streamed call | router call | buffered call |
|---|---|---|---|
| silent | ends at 2.1 s | 2.1 s | 2.1 s |
| headers, then silent | 2.1–2.2 s | 2.2 s | 2.1 s |
| one byte every second | **never** | **never** | never directly; 2.1 s via the proxy, which sends nothing downstream until the upstream finishes |

**Real Ollama is silent until its first token.** Headers, first byte and first
data line arrived together: at 101 s for a lone 5,361-token prompt, and at 208 s
for one queued behind it. So a slow or queued call is caught by the read timeout.
A stream that keeps moving is caught by nothing.

`settings.inference_call_deadline` wraps every model call the agent makes: each
answer-writing call, each router call, each escalation, each grounding retry. Each
call gets its own clock. A call that runs past it ends the turn with:
- `stop_reason="inference_error"`
- a final step reading `inference call stopped at the 900s per-call deadline`
- an answer saying the model did not finish in time. It does not say the model was
  unreachable, because it was reached.

Tool results gathered before the stopped call stay in the trace, and the answer
says so. Cancelling closes the HTTP response, so the proxy sees the disconnect and
closes its request upstream.

**Why 900 s.** The data is every Ollama request log on this host: 1,859 chat
calls, 10–14 September. Any call that overlapped laptop standby was removed (see
below).

| completed calls, machine awake | n | p99 | max |
|---|---|---|---|
| uncontended | 290 | 223 s | 225 s |
| queued behind other users | 1,390 | 306 s | 411 s |

- **Against completions:** 900 s is 2.2x the slowest completion on record, and 4x
  the slowest uncontended one.
- **Against failures:** no call that failed while the machine was awake ran past
  313 s.
- **What that means:** across that history, the deadline would never have cut off a
  call that was going to finish. It exists for the trickling stream, the one case
  the read timeout cannot end.

**The long calls on record were standby, not a broken timeout.** Three sets of
calls ran far past 300 s:
- 54 minutes, the live injection test during CI validation
- 24 min 55 s, the full-stack load test at 5 users
- 3 h 53 min, also at 5 users

They were first blamed on the timeout, then on paging. **Both explanations were
wrong.** The Windows power log shows every one spanned Modern Standby. The
54-minute call started at 15:09:25, the laptop entered standby at 15:10:33 and woke
at 16:03:28, and the call ended one second later. Nothing runs while the machine
is suspended, the deadline included. The page-in spikes that looked like paging
were Windows faulting memory back in on resume.

Long measurements on a laptop need the power log checked
(`Get-WinEvent` on Kernel-Power events 506/507 and 42/107), or their own sampling
gaps watched. `benchmarks/parallel_ab.py` now does the latter.

The GPU overlay leaves this at 900 s. That is loose for a GPU, and should come
down once there are measured GPU call times, the same way `INFERENCE_READ_TIMEOUT`
does there.
