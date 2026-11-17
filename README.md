# Finance Research Copilot

An agent that answers questions about public companies from their SEC 10-K filings,
SEC's structured XBRL figures and live market data, and **will not state a number
that no tool returned**.

Ask "What was NVIDIA's diluted EPS in fiscal 2026?" or "At its August 7, 2026 close,
what P/E does that EPS imply?":
- It looks the figures up with tools.
- It cites the filing each one came from.
- It ends with a single figure a scorer can read.

If it cannot ground a number, it says so instead of guessing.

FastAPI and an agent loop in Python, Postgres with pgvector, Redis, a Next.js
client. The model is pluggable: Qwen 2.5 on a laptop through Ollama, or
`openai/gpt-oss-120b` hosted on Groq.

## Results so far

Every number below links to the file that records how it was measured: the command,
the commit, the date and machine, and the raw output. Anything not yet measured is
marked **pending**.

### Answer accuracy (40-question eval)

| system | passed | source |
|---|---:|---|
| `gpt-oss-120b` on Groq, current code (`d106dae`, 2026-11-16) | **36/40** | [HOSTED_RUN_2026-11-16.md](benchmarks/HOSTED_RUN_2026-11-16.md) |
| `gpt-oss-120b` on Groq, before structured facts (2026-11-15) | 30/40 | [MODEL_COMPARISON.md](benchmarks/MODEL_COMPARISON.md) |
| `qwen2.5:7b` on a laptop CPU, baseline (2026-11-11) | 27/40 | [RUN_C_ANALYSIS.md](benchmarks/RUN_C_ANALYSIS.md) |
| `gpt-oss-120b`, with the arithmetic calculator | **pending**: scheduled for 2026-11-17 | |
| run-to-run noise of the hosted model | **pending**: one run per configuration so far | |

The hosted model's failures by root cause, between the two hosted runs
([HOSTED_RUN_2026-11-16.md](benchmarks/HOSTED_RUN_2026-11-16.md)):
- wrong tool: 3 to 0;
- grounding-guard refusals: 2 to 0;
- arithmetic: 2 to 1;
- retrieval misses: 3 to 3.

Every failure is classified by hand from its trace, by the rule in
[FAILURE_ANALYSIS.md](benchmarks/FAILURE_ANALYSIS.md).

### Latency and cost

| | measured | source |
|---|---|---|
| hosted, per question, wall clock | p50 26.3 s | [HOSTED_RUN_2026-11-16.md](benchmarks/HOSTED_RUN_2026-11-16.md) |
| hosted, without waits for the free tier's 8,000 tokens/min limit | p50 2.9 s, p95 8.0 s | same |
| hosted cost | $0.0016 per question (at Groq's published price, read 2026-11-15) | same |
| local `qwen2.5:7b` on CPU | p50 99.4 s | [MODEL_COMPARISON.md](benchmarks/MODEL_COMPARISON.md) |
| retrieval, `search_filings` over 45 filings | p50 66 ms, p95 88 ms | [STEP4_CORPUS.md](benchmarks/STEP4_CORPUS.md) |

### Scale and deployment

| | measured | source |
|---|---|---|
| corpus | 45 10-Ks of 15 companies, 2,914 chunks, 62,387 XBRL facts rows | [STEP4_CORPUS.md](benchmarks/STEP4_CORPUS.md) |
| ingestion from empty | 19.4 min on a laptop CPU; resumes after a crash and gives an identical index | same |
| retrieval hit@5, 2 filings against 45 | 100.0% against 75.9%: more years of each company confuse the year asked about | same |
| concurrent users, local model | 5 with no failures; the model is the ceiling between 5 and 10 (2026-09-13) | [LOAD_TEST_RESULTS.md](benchmarks/LOAD_TEST_RESULTS.md) |
| API memory under a 512 MB cap, answering a real question | 414.6 MiB peak, no out-of-memory kill | [CONTAINER_MEMORY.md](benchmarks/CONTAINER_MEMORY.md) |
| public demo on free tiers (Vercel, Render, Neon, Upstash) | **pending**: prepared ([DEPLOY.md](docs/DEPLOY.md)), not deployed | |
| fix for the wrong-year retrieval | **pending** | [KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) |

## Architecture

```mermaid
flowchart LR
    user([Browser]) --> web[Next.js client]
    web -->|"JWT, /ask/stream"| api

    subgraph server [FastAPI process]
        api[API: auth, rate limit, daily caps] --> loop[Agent loop]
        loop --> guards["Guards: grounding, investment advice, untrusted content"]
        loop --> proxy["/v1 model proxy (holds the provider key)"]
        loop --> tools
        subgraph tools [Tools]
            facts[get_financial_fact]
            search[search_filings]
            ratio[calculate_ratio]
            price[get_stock_price]
            news[search_news]
        end
    end

    proxy --> llm[("Groq gpt-oss-120b<br/>or Ollama Qwen 2.5")]
    facts --> pg[(Postgres)]
    ratio --> pg
    search --> emb[bge-small embeddings] --> pg
    price --> yf[Yahoo Finance]
    news --> gn[Google News RSS]
    api --> pg
    api --> redis[(Redis)]

    subgraph offline [Offline ingestion]
        edgar[SEC EDGAR] --> parse[parse 10-K, chunk, embed] --> pg
        edgar --> xbrl[XBRL company facts] --> pg
    end
```

| part | what it does |
|---|---|
| **Agent loop** (`agent/orchestrator.py`) | Up to five model rounds a question. Locally, a small router model picks tools and a larger one writes the answer; hosted, one model does both. |
| **Grounding guard** (`agent/grounding.py`) | Every number in a draft answer must appear in a tool result from that turn. One that does not is sent back to the model once, then shown under an `[Unverified]` caveat. It is never presented as fact. |
| **Advice and injection guards** (`agent/advice.py`, `agent/untrusted.py`) | No buy or sell recommendations. Filing text and news are fenced as data, so instructions embedded in them are not followed. |
| **Retrieval** (`rag/`) | pgvector with an HNSW index, metadata filters applied before the top-k, and a lexical and table-affinity re-rank. |
| **Structured facts** (`tools/facts.py`) | SEC XBRL figures by company, concept and fiscal year, each with the accession number of its filing as the citation. |
| **Model proxy** (`api/inference_proxy.py`) | One OpenAI-compatible endpoint in front of Ollama, Groq or Cerebras. The provider key never leaves the server. |
| **Eval** (`eval/`) | 40 questions with tolerances; a retrieval-only eval; a fast 15-question subset; a CI gate. |

## How it was built

A few practices run through the repository:
- **Every measurement has a record.** A claim about accuracy, latency or memory
  comes from a command run on the code it describes. It is written up in
  `benchmarks/` with the commit, machine and raw output, and labelled measured,
  inferred or not run.
- **The goalposts do not move.** The eval questions, tolerances and the scorer
  are changed only with sign-off and never in the same commit as an agent change.
  The grounding guard's rules are protected the same way.
- **Corpora are pinned.** Filings by accession number, facts by date, so "the
  latest 10-K" cannot silently change what a result means.
- **Known limitations are written down, not hidden.**
  [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) lists what looks like
  a bug, why it is that way, and what would justify changing it.

## Running it

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows; source .venv/bin/activate elsewhere
pip install -r requirements.txt
copy .env.example .env                               # set SEC_EDGAR_USER_AGENT; see the file

docker compose -f docker-compose.dev.yml up -d       # Postgres with pgvector, and Redis
python -m alembic upgrade head
python scripts/index_filings.py && python scripts/index_facts.py   # the eval's two filings
# or the 15-company corpus: python scripts/index_corpus.py

ollama pull qwen2.5:7b && ollama pull qwen2.5:1.5b   # or set AGENT_PROVIDER=groq in .env
uvicorn api.main:app --port 8000
cd frontend && npm install && npm run dev
```

```bash
python -m pytest -p no:cacheprovider -q -rs          # the test suite
python -m eval.run_eval --subset fast                # 15 questions against the running API
python -m eval.retrieval_eval                        # retrieval only, no model
```

## Documentation

- [docs/PROJECT.md](docs/PROJECT.md): the full design write-up
- [docs/DATABASE.md](docs/DATABASE.md): schema, auth, migrations
- [docs/STRUCTURED_FACTS.md](docs/STRUCTURED_FACTS.md): the XBRL facts tool
- [docs/INFERENCE.md](docs/INFERENCE.md): the model proxy and providers
- [docs/RATE_LIMITING.md](docs/RATE_LIMITING.md): the Redis sliding-window limiter
- [docs/DEPLOY.md](docs/DEPLOY.md): the public demo on free tiers
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): the full stack on one GPU host
- [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md)
- [benchmarks/](benchmarks/): every measurement, with how it was taken
