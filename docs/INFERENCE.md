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
