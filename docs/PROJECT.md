# Finance Research Copilot

An LLM agent that answers questions about public companies. It combines
**retrieval over SEC 10-K filings** with **live tools** for stock prices,
financial ratios and news, and it will not state a figure that a tool did not
return in the same turn.

This document describes what the code does today. Deliberate behaviour that
looks like a defect is covered in [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md);
the subsystem docs are linked at the end.

_Last brought in line with the code on 2026-11-10, at commit `cea0dd7`._

---

## 1. What it does

A signed-in user asks a question in the web client, for example:

> "What was NVIDIA's gross margin, and how does Apple's compare?"

The agent decides which tools it needs. It can search the indexed filings, fetch
prices, compute ratios or pull headlines, and it runs several calls in parallel
when they are independent. It then answers in prose, stating each figure with
its company and period. The answer streams to the browser as it is written, and
the whole turn is stored with its full trace.

**Scope today:**
- **Filings:** two filings are indexed, NVIDIA's FY2026 10-K (period ending
  2026-01-25) and Apple's FY2025 10-K (period ending 2025-09-27). From each,
  only Item 1A (Risk Factors), Item 7 (MD&A) and the consolidated income
  statement are ingested.
- **Live tools:** prices, ratios and news work for any listed ticker.
- **Hard-coded scope:** the system prompt names these two filings and their
  fiscal years (`agent/prompts.py`). Re-indexing a newer filing therefore also
  means updating the prompt and regenerating `eval/questions.json`.

**What it will not do:** give investment advice. Questions about whether to buy,
sell or hold, valuation verdicts and price forecasts are redirected. See
§3.3.

---

## 2. Architecture

```
  Browser ── Next.js client (frontend/)
     │  JWT bearer token, POST /ask/stream (SSE over fetch)
     ▼
  ┌──────────────────────────── FastAPI process (api/) ─────────────────────────────┐
  │  RateLimitMiddleware (Redis sliding window)  →  routes                          │
  │    /auth/*  /conversations/*  /ask  /ask/stream  /health*  /v1/* (proxy)       │
  │                     │                                                           │
  │                     ▼                                                           │
  │              agent/orchestrator.run_agent ──── HTTP ───▶ /v1/chat/completions ──┼──▶ Ollama
  │                │            │                         (this same process)      │   qwen2.5:7b
  │                ▼            ▼                                                   │   qwen2.5:1.5b
  │        rag/ (search_filings)   tools/ (price, ratio, news)                      │
  └────────────────┼────────────────────┼───────────────────────────────────────────┘
                   ▼                    ▼
        Postgres: users, conversations,  yfinance, Google News RSS
        messages, filing_chunks
                   ▲
                   │ offline
        ingestion/ ── EDGAR → parse → chunk → embed (bge-small, CPU) → upsert
```

**Two runtimes:**
- **Offline:** `scripts/index_filings.py` drives `ingestion/pipeline.py` to build
  the `filing_chunks` table, and `scripts/index_facts.py` fills `xbrl_facts`
  from SEC's company-facts API. Nothing schedules them; they are run by hand,
  and by CI on every run. Both are pinned for the eval companies: the filings
  by accession number, the facts as filed through a date.
- **Online:** the API serves requests. The agent calls the inference server
  through the app's own `/v1` proxy rather than calling Ollama directly, so the
  same timeouts and logging apply to it. It sends a per-process `X-Internal-Token`
  header so its own calls are not counted against the user's rate limit.

**Optional:** `inference_router/` is a separate round-robin process that can sit
between the proxy and several identical inference replicas. It was built and
measured but is not used by default; see
[INFERENCE_REPLICAS.md](INFERENCE_REPLICAS.md).

---

## 3. Request lifecycle

### 3.1 The endpoint (`api/routes.py`)

`POST /ask` returns the finished turn. `POST /ask/stream` reports the same run as
Server-Sent Events. Both use the database in three phases, and hold no
connection during the slow middle one:

1. **Read** (milliseconds). Check that the caller owns the conversation, and
   load the last `AGENT_HISTORY_MESSAGES` (10) user and assistant messages.
2. **Think** (minutes on CPU). Run the agent with no database connection held.
3. **Write** (milliseconds). Store the question and the answer in one
   transaction, with the trace in `messages.meta`. This step is retried on
   pool exhaustion, because the answer cannot be regenerated cheaply.

The stream's run is a background task that is **not** cancelled when the client
disconnects. A refresh does not lose a finished answer; it can produce
duplicate turns instead (see KNOWN_LIMITATIONS.md).

Stream event types: `iteration`, `escalate`, `tool_start`, `tool_result`,
`token`, `discarded`, `unverified`, `blocked`, `budget`, `done`, `error`.

### 3.2 The agent loop (`agent/orchestrator.py`)

One iteration is one model call plus every tool call that model call asked for.

- **Two models.** The 1.5b router model picks tools. The moment it starts
  writing prose instead, the turn is handed to the 7b model to write the
  answer. The 7b model also takes over the step after any failed tool call,
  and after a grounding retry, because the 1.5b model was measured unable to
  recover from a bad-input error. Setting `AGENT_ROUTER_MODEL=""` runs every
  step on 7b.
- **Limits:**
  - `AGENT_MAX_ITERATIONS=5` model round-trips.
  - `AGENT_MAX_TOOL_CALLS_PER_ITERATION=8`. Calls beyond the budget are not
    run, and the turn ends with an explicit "too many calls" message.
  - `max_tokens` is 512 for 7b and 128 for the router.
  - `INFERENCE_CALL_DEADLINE=900` s caps each model call's wall-clock time.
- **Tool results are compacted** before the model sees them
  (`agent/tools.py`): at most 8 headlines, a price summary instead of the
  daily bars, and filing excerpts cut to 700 characters. The full result is
  kept in the trace.
- **Tool results are framed as untrusted** (`agent/untrusted.py`). Each one is
  wrapped in `<tool_result>` tags, and any tag sequence inside the content is
  escaped, so a filing or headline cannot close the block and speak as the
  operator.
- **Every ending is labelled** with a `stop_reason`:
  - `final_answer`
  - `unverified_figures`
  - `investment_advice`
  - `tool_call_budget`
  - `max_iterations`
  - `inference_error`
  - `empty_response`

  `completed` is true only for a clean `final_answer`.

### 3.3 The two output guards

**Grounding** (`agent/grounding.py`, backed by `core/numerics.py`):
- **What counts as a figure:** every number in a draft answer, except dates,
  years and form names such as "10-K".
- **What supports a figure:** it must be traceable to the *output* of a tool
  call that succeeded *in this turn*. Rescaling counts (0.7107 matches 71.07%,
  dollars match the same amount in billions). So does one arithmetic step over
  two evidence values, such as revenue derived as gross profit divided by
  margin.
- **What does not count:** arguments the model passed, failed calls and earlier
  turns.
- **On failure:** the draft is discarded once, and the model is told exactly
  which figures failed. If figures are still unsupported after that retry, the
  answer is returned with an `[Unverified: ...]` prefix and
  `completed=false`.

**Advice** (`agent/advice.py`):
- **What it matches:** regexes for directives ("you should buy"), verdicts
  ("a strong buy", "undervalued") and price forecasts.
- **What it lets through:** attributed statements ("analysts rate it...") and
  the model's own refusals.
- **What happens on a match:** the answer is *replaced* with a redirection that
  lists whatever the tools did establish. There is no retry.

The system prompt asks for both behaviours too. The guards exist because the
prompt alone was measured not to hold.

### 3.4 The tools

| tool | source | notes |
|---|---|---|
| `get_financial_fact` | `xbrl_facts`, from SEC XBRL company facts | one reported figure by company, named fact and fiscal year; returns the filing's accession number as the citation. Registered first; numeric questions are routed to it. See [STRUCTURED_FACTS.md](STRUCTURED_FACTS.md) |
| `search_filings` | `rag/retriever.py` over `filing_chunks` | BGE query embedding, SQL-side filters (company, section), over-fetch then hybrid re-rank (lexical overlap + table boost for figure-seeking questions) |
| `get_stock_price` | yfinance | inclusive `end_date`, optional; split/dividend-adjusted; summary fields always cover the full range |
| `calculate_ratio` | yfinance statements; `xbrl_facts` when `fiscal_year` is given | 7 ratios; returns formula and raw inputs; `pe_ratio` from Yahoo's trailing P/E. With `fiscal_year`, margins, return on equity and current ratio come from that year's facts |
| `calculate` | code (`tools/calculator.py`) | one step of arithmetic on two operands: add, subtract, multiply, divide, percent_change, percent_of. An operand is a fact reference (`NVDA:revenue:2026`) or a number a tool returned this turn; a number no tool returned is refused before the tool runs. Every derived figure goes through it: the grounding guard accepts a figure only if a tool result contains it |
| `search_news` | Google News RSS | no API key; `days_back` is filtered locally, not by the feed |

Schemas are generated from each function's signature and docstring
(`tools/registry.py`). `Literal` types become enums, and arguments are coerced
toward their declared types. Every tool returns an `{"ok": ...}` envelope and
never raises. Sync tools run in a thread so they do not block the event loop.

---

## 4. Folder responsibilities

| path | responsibility |
|---|---|
| `api/` | FastAPI app (`main.py`), auth / conversation / agent routes, the OpenAI-compatible inference proxy, the rate-limit middleware |
| `agent/` | loop, system prompt, tool wiring and compaction, conversation memory, grounding and advice guards, untrusted-content framing |
| `rag/` | BGE embeddings (no sentence-transformers), vector store with pgvector or `real[]` fallback, retriever and re-ranker |
| `ingestion/` | EDGAR client (validated User-Agent, rate limit, retry, disk cache), 10-K parser, token-accurate chunker, XBRL company-facts parser |
| `tools/` | the three live tools, the structured facts tool and its storage, the result envelope, the schema-deriving registry, yfinance network settings |
| `auth/`, `db/` | bcrypt + HS256 JWT; async SQLAlchemy engine, sessions, ORM models |
| `core/` | typed settings (`config.py`), shared number extraction (`numerics.py`), retry with jittered backoff (`retry.py`) |
| `migrations/` | Alembic: `0001` users/conversations/messages, `0002` `filing_chunks` (probes for pgvector, falls back to `real[]`), `0003` `xbrl_facts` |
| `inference_router/` | optional round-robin router over inference replicas |
| `eval/` | 40-case numeric question set, end-to-end runner, deterministic scorer, baseline comparison, retrieval-only eval, groundedness judge |
| `frontend/` | Next.js 16 / React 19 chat client: login, signup, conversation sidebar, streamed answers with sources |
| `scripts/` | demos (ingest, chunk, search, agent), indexing, smoke and deployment checks, migration drift check, CI JUnit gate |
| `benchmarks/` | Locust full-stack load test, replica and `NUM_PARALLEL` A/B drivers, written-up results |
| `deploy/`, `docker-compose*.yml`, `Dockerfile` | container stack (CPU default, GPU and vLLM overlays) and a RunPod fallback script |
| `.github/` | `tests.yml` and `eval-gate.yml`, sharing the `prepare-stack` composite action |

Dependencies point one way: `api/` → `agent/` → {`rag/`, `tools/`}. `rag/`
reads what `ingestion/` wrote, through the database only. `eval/` drives the
running API over HTTP.

---

## 5. External dependencies, as actually used

| dependency | used for | notes |
|---|---|---|
| **Ollama** serving `qwen2.5:7b` and `qwen2.5:1.5b` | all inference | any OpenAI-compatible server works behind the proxy; CI pins Ollama 0.34.0 |
| **SEC EDGAR** | filings | no key; requires a real contact in `SEC_EDGAR_USER_AGENT`, which is validated structurally |
| **yfinance** | prices, statements, P/E | its built-in retry loop is switched on (it ships disabled) |
| **Google News RSS** | headlines | no key |
| **PostgreSQL 16** (+ pgvector when available) | app data and vectors | one database for both |
| **Redis** | rate limiting | fails open by default |
| **`BAAI/bge-small-en-v1.5`** | embeddings, CPU | 384 dimensions |
| **Qwen2.5 tokenizer** | chunk sizing | Hugging Face |

**Not used**, despite appearing in setup files:
- **Claude / Anthropic.** `anthropic` is in `requirements.txt` but nothing
  imports it, and earlier versions of this document named Claude as the default
  LLM.
- **Unread `.env.example` settings.** `ANTHROPIC_API_KEY`, `LLM_MODEL`,
  `EMBEDDING_*`, `VECTOR_STORE*`, `MARKET_DATA_*`, `NEWS_API_*`, `API_HOST`,
  `API_PORT` and `LOG_LEVEL` are not read by `core/config.py`.

---

## 6. Status

### Built and working end to end

| | what |
|---|---|
| M0–M3 | scaffold; inference proxy with byte-for-byte SSE relay; users, JWT auth, conversations, keyset-paginated history; Redis sliding-window rate limiting; cross-user isolation tests |
| M4 | EDGAR ingestion for NVDA and AAPL: structure-based section location, per-row unit scaling on the income statement, page-furniture stripping, sentence-boundary chunking (~500 Qwen tokens, ~50 overlap), tables kept whole with a natural-language caption |
| M5 | embeddings, vector store, retriever with SQL-side filters and hybrid re-ranking |
| M6 | the three tools, the registry, the agent loop, `POST /ask` |
| audit fixes F1–F4, F7 | per-row table scale, table captions + re-ranking, structural User-Agent validation, tool-call budget, conversation memory |
| after M6 | streaming endpoint and Next.js client; per-figure grounding guard; router/answer model split; advice and injection guards; explicit timeouts and retries; pool-exhaustion 503 and connection-free agent runs; per-call inference deadline; containerisation; CI test and eval gates |

### Measured

These numbers are taken from the documents and commits named in the last
column. Most were measured on the Windows CPU laptop described in
[INFERENCE.md](INFERENCE.md).

| what | result | where recorded |
|---|---|---|
| eval accuracy, baseline (Run A) | **18/40 = 45.0%** | `eval/baselines/current.json`. Ran at `787276f` on 7b for every step, before the router split; its configuration block is reconstructed, not recorded by the run |
| eval accuracy, first CI run (`012aaa3`) | 20/40 = 50.0%, read as run-to-run noise | [CI.md](CI.md) |
| eval noise, same system twice | 38.9% vs 33.3% on the first 18 cases | `eval/compare.py`, [baselines README](../eval/baselines/README.md) |
| retrieval, 29 filing cases, hybrid ranker | hit@3 86.2%, hit@5 100% (69.0% / 96.6% without re-rank) | commit `2f4ef31` message only; no saved output |
| single-user latency, full stack | median 86 s per answer, TTFT 37 s, 0.68 answers/min (13 runs) | [LOAD_TEST_RESULTS.md](../benchmarks/LOAD_TEST_RESULTS.md) |
| concurrency | delivered throughput does not rise with users. Raw Ollama gives 1.15x throughput at 4x concurrency | same |
| router model | routing step 13.7 s → 3.7 s; 1.5b decodes 13.9 tok/s vs 7b's 2.8 | `core/config.py` comment |
| embedding | 113 chunks in 19.9 s; warm query 33–36 ms | earlier revision of this file, 2026-09-04 |
| CI duration | eval gate 50.6 min on the 4 vCPU / 16 GB public runner; test gate ~12 min | [CI.md](CI.md) |

### Built but never executed

- **vLLM and any GPU path.** `docker-compose.vllm.yml`,
  `docker-compose.gpu.yml` and `deploy/pod-bootstrap.sh` exist, but the dev
  host cannot run vLLM ([INFERENCE.md](INFERENCE.md)), so continuous batching
  is unverified.
- **The `docker-compose.yml` CPU stack.** It is not yet run. Docker is not
  installed on the dev host ([DEPLOYMENT.md](DEPLOYMENT.md) §8), and every
  load test ran against host processes.
- **The pgvector HNSW index.** The dev host runs the `real[]` fallback.
  pgvector is present in CI's Postgres image, but its performance there has not
  been measured.
- **`OLLAMA_NUM_PARALLEL=2`.** The full-stack A/B was inconclusive because the
  laptop went into standby during the run, so the setting was not applied
  ([INFERENCE_REPLICAS.md](INFERENCE_REPLICAS.md)).

### Not built

- **Coverage:** filings beyond two tickers and one 10-K each, and the balance
  sheet and cash-flow statement from filings. Balance-sheet ratios come only
  from yfinance.
- **Scheduled re-ingestion:** none. The index is rebuilt only by hand or by CI.
- **Token revocation:** none (stateless JWTs, 30-minute expiry), and no refresh
  tokens.
- **In-flight run deduplication:** none.
- **Citations as structured fields:** the stored trace carries `source_url`
  for filing hits, but the API response has no citation list.

### Tests and CI

`pytest` collects **926 tests**, 106 of them marked `safety`. Safety tests are
properties the agent must never violate, and a failure blocks a release.

Every external dependency becomes a skip when it is absent. That covers
Postgres, Redis, Ollama, the models and the index, so a laptop run is partial
unless the whole stack is up.

CI runs on GitHub Actions; see [CI.md](CI.md):
- **`tests.yml`** runs on every push. It fails on any failure, on any skip, and
  on fewer than 900 tests.
- **`eval-gate.yml`** runs on changes to `agent/`, `rag/`, `tools/` or `eval/`.
  It fails when accuracy falls below the baseline by more than the 2-case noise
  band.

---

## 7. Further reading

- [DATABASE.md](DATABASE.md): schema, auth, migrations, where the local Postgres lives
- [RATE_LIMITING.md](RATE_LIMITING.md): the sliding-window limiter and its fail-open policy
- [INFERENCE.md](INFERENCE.md): inference servers, Ollama settings and the per-call deadline
- [INFERENCE_REPLICAS.md](INFERENCE_REPLICAS.md): the replica router and the `NUM_PARALLEL` measurements
- [DEPLOYMENT.md](DEPLOYMENT.md): containers and cloud GPU hosts
- [DEPLOY.md](DEPLOY.md): the public demo on free tiers (Render, Neon, Upstash, Vercel), with the daily caps that protect the Groq allowance
- [CI.md](CI.md): the two gates and how to roll back
- [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md): deliberate trade-offs
- [../benchmarks/LOAD_TEST_RESULTS.md](../benchmarks/LOAD_TEST_RESULTS.md): the full-stack load test
