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

Last updated 2026-09-04, after milestone 5's retrieval layer landed.
**This section is behind the code**: milestone 6 (tools, the agent loop and
`POST /ask`) and audit fixes F1-F4 and F7 have since landed. See the git log
until this is rewritten.

Deliberate behaviour that looks like a defect until you know why is in
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md), not here; this section is for
what is and is not built.

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

### Milestone 4 — ingestion: in progress

Implemented, for two tickers (NVDA, AAPL) and their most recent 10-K each:

**`ingestion/edgar_client.py`** — ticker → CIK via SEC's ticker map, latest
filing via `data.sec.gov/submissions/`, document download from `/Archives/`.
Rate limited below SEC's published 10 req/s ceiling; filings cached under
`data/edgar/` (gitignored) so reruns cost SEC nothing. Exact form matching, so a
`10-K/A` amendment is never returned in place of the 10-K.

**`ingestion/parser.py`** — sections located by document structure, not by regex
over flattened text. The real Item heading is a body-level block with no
`<table>` ancestor; the table-of-contents copy is always inside a table. Extracts
Item 1A, Item 7, and the consolidated income statement as separate structured
objects. Page furniture (bare page numbers, "Table of Contents", running
footers, and any short line repeated ≥5×) is stripped, and sentences the filing
split across a page boundary are stitched back together.

**`ingestion/chunker.py`** — prose packed to ~500 tokens with ~50 token overlap
using the real Qwen2.5-1.5B-Instruct tokenizer, on paragraph/sentence
boundaries. Financial tables are emitted as exactly one chunk each and never
split. Every chunk carries `company`, `filing_type`, `fiscal_period`, `section`,
`chunk_index`, plus provenance back to the sec.gov URL.

Current output: **NVDA 72 chunks, AAPL 41**, zero mid-sentence splits.

**`ingestion/pipeline.py`** — fetch → parse → chunk → embed → store, for one
ticker. Idempotent: chunk ids derive from the accession number and the store
upserts on them, so re-running after a chunker change updates rows in place
instead of leaving two generations competing for the top-k.

Multi-year and multi-ticker coverage is deferred; the current scope is
deliberately two filings.

### Milestone 5 — retrieval: embeddings, vector store, retriever

**`rag/embeddings.py`** — `BAAI/bge-small-en-v1.5`, 384 dimensions, 33M
parameters, on CPU. No `sentence-transformers` dependency: for this model that
library's forward pass is CLS pooling plus an L2 norm. Two entry points, because
BGE is asymmetric — queries take the retrieval instruction prefix, passages do
not, and getting it backwards costs recall without erroring.

Chunks are sized in *Qwen* tokens and BGE's tokenizer disagrees, so ~13% of them
overflow its 512-token limit (longest measured: 560). Rather than let the
tokenizer drop the tail, an over-long passage is embedded in overlapping windows
and pooled into one renormalized vector.

**`rag/vector_store.py`** — one `filing_chunks` table in the existing Postgres.
No separate vector service: at this size it would be pure operational cost, and
co-locating vectors with metadata makes a filter a WHERE clause the planner
applies *before* the top-k rather than a post-filter over an approximate result
set.

**`rag/retriever.py`** — `search_filings(query, company=None, section=None)`.
Filters push into SQL; `company` accepts a ticker or a name substring and
`section` accepts shorthand (`"risk"`, `"mdna"`, `"income"`).

Measured on this machine, 113 chunks from the two filings:

| stage | measurement |
|---|---|
| embedding, 113 chunks | **19.9 s** — 176 ms/chunk, 5.7 chunks/s, CPU |
| encoder load (once per process) | 11.7 s |
| query latency, warm | 33-36 ms (embed + search) |
| first query after load | ~2.0 s (torch's first forward pass) |

**Retrieval quality is good on prose and weak on tables.** "What are the main
competitive risks?" returns three on-topic Risk Factors passages (0.719-0.722),
and the same query filtered to NVDA returns NVDA's risk-factor summary first
(0.690). But "What was total revenue?" ranks the income statement **5th**
(0.646) behind MD&A prose *about* revenue (0.740) — a rendered table is mostly
digits, and a conversational question embeds close to sentences, not to numbers.
`content_type="table"` reaches it directly, and a hybrid keyword/vector retrieval
or a table-aware summary line is the real fix. Not done yet.

### pgvector, and the fallback this machine runs

The intended column type is `vector(384)` with an HNSW index, and that is what
`alembic upgrade head --sql` emits. **The development host cannot install
pgvector** — it is a C extension, the portable Postgres 16.9 here has no
`vector.control`, there is no MSVC toolchain to build it, and no Docker. So
migration `0002` probes `pg_available_extensions` and falls back to a plain
`real[]` column, over which `rag/vector_store.py` computes the dot product in
SQL.

Because embeddings are L2-normalized, cosine similarity equals the inner
product, and **both backends return identical scores** — the fallback changes
how fast a search runs, not what it returns, which is what makes the numbers
above worth reporting. What it does not have is an ANN index: every search is a
sequential scan. Fine at 113 rows, useless at a million. **HNSW is therefore
unverified**, exactly like continuous batching below.

### Milestones 6+ — not started

`tools/`, `agent/`, `eval/`, and `api/routes.py` are still docstring-only stubs.
**There is no `/ask` endpoint** — the copilot's actual product surface. Retrieval
now works end to end from the command line, but nothing serves it over HTTP and
no agent reasons over what it returns.

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
