# Finance Research Copilot

An LLM-powered research agent that answers questions about public companies by
combining **RAG over SEC filings** with **live tools** (stock price, ratio
calculation, news search).

> **Status:** early. The inference proxy and the persistence/auth layer are
> implemented; ingestion, RAG, tools, and the agent are still stubs.

## Architecture

See [docs/PROJECT.md](docs/PROJECT.md) for the full write-up. In short:

```
api/  →  agent/  →  { rag/ , tools/ }
  ↓                    rag/  ← ingestion/ (offline index build)
{ auth/ , db/ }
eval/ drives the whole stack
```

## Layout

| Path         | Purpose |
|--------------|---------|
| `ingestion/` | Offline pipeline: fetch → parse → chunk → embed SEC filings into the vector store. |
| `rag/`       | Query embedding, vector search, filtering, re-ranking, context assembly. |
| `tools/`     | Callable live capabilities: stock price, financial ratios, news search. |
| `agent/`     | Orchestration loop: planning, tool routing, memory, cited synthesis. |
| `api/`       | FastAPI transport layer: routes, schemas, validation, streaming. |
| `auth/`      | Password hashing, JWT issuance, the current-user dependency. |
| `db/`        | SQLAlchemy engine, session lifecycle, and the ORM models. |
| `core/`      | Typed settings loaded from the environment. |
| `migrations/`| Alembic revisions. See [docs/DATABASE.md](docs/DATABASE.md). |
| `eval/`      | Question sets + metrics that exercise the full stack. |

## Getting started

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env         # then fill in keys

docker compose -f docker-compose.dev.yml up -d   # Postgres + Redis
python -m alembic upgrade head                  # create the schema

uvicorn api.main:app --reload
```

- [docs/DATABASE.md](docs/DATABASE.md) — schema design, auth, migrations
- [docs/RATE_LIMITING.md](docs/RATE_LIMITING.md) — Redis sliding-window limiter
- [docs/INFERENCE.md](docs/INFERENCE.md) — running a model behind the proxy
