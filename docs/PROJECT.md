# Finance Research Copilot

An LLM-powered agent that answers questions about public companies by combining
**retrieval over SEC filings (RAG)** with **live tools** (stock price, financial
ratio calculation, news search).

---

## 1. What it does

A user asks a natural-language question, e.g.:

> "How has Apple's gross margin trended over the last three years, and how does
> today's valuation compare to that trend?"

The copilot:

1. Decides which knowledge sources it needs (filings, live market data, news).
2. Retrieves relevant passages from indexed SEC filings.
3. Calls live tools for anything time-sensitive or computational.
4. Synthesizes a grounded answer with citations back to the source filing and
   the tool outputs it used.

---

## 2. High-level architecture

```
                         ┌─────────────────────────┐
   HTTP request  ───────▶│          api/           │  FastAPI: request/response,
                         │  (transport + schemas)  │  auth, streaming, sessions
                         └───────────┬─────────────┘
                                     │
                                     ▼
                         ┌─────────────────────────┐
                         │         agent/          │  Orchestration loop:
                         │  plan → act → observe   │  routing, tool selection,
                         │        → answer         │  memory, final synthesis
                         └───┬─────────────┬───────┘
                             │             │
                 ┌───────────▼──┐      ┌───▼───────────────┐
                 │     rag/     │      │      tools/       │
                 │  retrieval   │      │  stock price,     │
                 │  over the    │      │  ratio calc,      │
                 │  vector DB   │      │  news search      │
                 └───────┬──────┘      └──────────────────┘
                         │
                 ┌───────▼──────┐
                 │  ingestion/  │  Offline pipeline: fetch filings from EDGAR,
                 │  (offline)   │  parse, chunk, embed, write to vector store
                 └──────────────┘

   eval/  ── exercises the whole stack against a fixed question set + metrics
```

**Two runtimes:**

- **Offline / batch** — `ingestion/` builds and refreshes the filing index. Run
  on a schedule (e.g. nightly, or on new-filing webhooks).
- **Online / request-time** — `api/` → `agent/` → (`rag/` + `tools/`) serves user
  questions.

---

## 3. Request lifecycle (online path)

1. **`api/`** receives the question, validates it, resolves the session, and
   hands a normalized request to the agent.
2. **`agent/`** runs its reasoning loop:
   - Plan: what does answering this require?
   - Retrieve: query `rag/` for filing context.
   - Act: call `tools/` for live price, ratios, or news as needed.
   - Observe: fold results back into context; loop if more is needed.
   - Answer: synthesize a cited response.
3. **`api/`** streams or returns the answer plus structured citations.

---

## 4. Folder responsibilities

| Folder        | Layer            | Responsibility |
|---------------|------------------|----------------|
| `ingestion/`  | Offline pipeline | Pull filings from SEC EDGAR, parse HTML/XBRL, clean, chunk, embed, and persist to the vector store. Owns the "how do documents get into the index" problem. |
| `rag/`        | Retrieval        | Everything about turning a query into relevant filing passages: embedding the query, vector search, filtering by company/form/period, re-ranking, and packaging context for the agent. |
| `tools/`      | Live capabilities| Discrete, callable functions the agent can invoke: current/historical stock price, financial-ratio computation, news search. Each tool has a typed input/output contract and no knowledge of the agent. |
| `agent/`      | Orchestration    | The decision-making core: prompt construction, tool routing, the plan/act/observe loop, conversation memory, and final answer synthesis with citations. |
| `api/`        | Transport        | FastAPI application: HTTP routes, request/response schemas, validation, auth, session handling, streaming. Thin — delegates all thinking to `agent/`. |
| `eval/`       | Quality          | Regression and quality harness: curated question sets, expected-answer/ground-truth data, and metrics (retrieval hit rate, answer faithfulness, tool-call correctness, latency). Runs the full stack. |

Dependency direction: `api/ → agent/ → {rag/, tools/}`; `rag/` consumes what
`ingestion/` produces (via the vector store, not a direct import). `eval/` sits
on the outside and drives `api/`/`agent/`.

---

## 5. External dependencies (planned)

- **SEC EDGAR** — filing source (full-text search + document API).
- **Vector store** — pluggable; local (e.g. Chroma/FAISS) for dev, hosted for prod.
- **Embedding model** — provider-configurable via env.
- **LLM** — defaults to Claude (`claude-sonnet-5`); provider/model set via env.
- **Market data API** — stock price / fundamentals (e.g. an equities data vendor).
- **News API** — headline/article search.

All credentials come from environment variables — see `.env.example`.

---

## 6. Status

Last updated 2026-09-04, after an evidence-based audit of milestones 0-3.

### Milestones 0-3 — complete

**M0 — Project setup.** Package layout above, `docs/`, `pyproject.toml`,
`requirements.txt`, `.env.example`, and a `.gitignore` that excludes `.env`
(verified via `git check-ignore`). Under git as of the milestone 0-3 commit.

**M1 — Inference path understanding.** `llm-internals/transformer_walkthrough.py`
(a sibling directory, *not* part of this repo): tokenization, a single forward
pass with `output_hidden_states`, and a hand-written greedy decode loop —
`model.generate()` deliberately unused. Verified end to end on
`Qwen/Qwen2.5-1.5B-Instruct`, float32 on CPU: 29 hidden states of shape
`(1, 35, 1536)`, a `(1, 2, 35, 128)` per-layer KV cache across 28 layers, and the
cache growing exactly one row per decode step. `--compare-nocache` measured
3,398 ms cached vs 8,259 ms uncached for 12 tokens.

**M2 — Inference server.** `api/inference_proxy.py` fronts any OpenAI-compatible
server: liveness (`/health`) split from upstream readiness (`/v1/health`),
`/v1/models` passthrough, and buffered plus streaming `/v1/chat/completions`.
SSE is relayed byte-for-byte and verified token-by-token against a live model
(~40 ms between frames, `X-Accel-Buffering: no`, no `Content-Length`).
`scripts/load_test.py` measures TTFT/TPOT/throughput with a concurrency sweep;
`scripts/smoke_test.py` gates CI.

**M3 — Backend: users, auth, chat history.** Postgres via async SQLAlchemy 2.0
with Alembic (`0001_initial`: `users`, `conversations`, `messages`); JWT signup /
login / me with bcrypt; conversation and message endpoints with keyset-paginated
history; Redis sliding-window rate limiting as ASGI middleware. Cross-user
isolation is enforced and tested — `tests/test_user_isolation.py`. See
`docs/DATABASE.md` and `docs/RATE_LIMITING.md`.

### Milestones 4+ — not started

`ingestion/`, `rag/`, `tools/`, `agent/`, `eval/`, and `api/routes.py` are still
docstring-only stubs (23 modules). **There is no `/ask` endpoint** — the copilot's
actual product surface, and everything in sections 1-3 above that describes
retrieval and tool use, remains unimplemented. What exists today is the platform
the agent will sit on, not the agent.

### Inference backend: Ollama, not vLLM

**vLLM cannot run on this machine and never has.** It ships Linux-only wheels
with no native Windows build, and its CPU backend must be compiled from source
on Linux. The development host is Windows 11 with Intel Iris Xe integrated
graphics — no NVIDIA GPU, no WSL. Separately, Qwen2.5-7B at bf16 is ~15.2 GB of
weights against 15.7 GB of system RAM.

**Ollama is the inference backend until a later milestone deploys to a cloud
GPU.** `docker-compose.vllm.yml` and the vLLM sections of `docs/INFERENCE.md`
are committed configuration that has **never been executed** — treat them as a
deployment target, not a verified path.

The practical consequence is that Ollama serves requests one at a time. Measured
through the proxy against `qwen2.5:1.5b` on CPU:

| concurrency | mean TTFT | p95 TTFT | system throughput |
|---|---|---|---|
| 1 | 0.40 s | 0.40 s | 16.1 tok/s |
| 5 | 13.10 s | 27.11 s | 16.1 tok/s |
| 10 | 25.06 s | 50.28 s | 21.0 tok/s |

Flat throughput with TTFT rising linearly is the signature of a queue, not a
batch. **Continuous batching is therefore unverified**, and these numbers are the
baseline that will make the eventual vLLM comparison meaningful.
