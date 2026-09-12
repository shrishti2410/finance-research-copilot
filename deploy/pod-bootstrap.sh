#!/usr/bin/env bash
#
# Bring the whole stack up inside a RunPod Pod, or any GPU container with no
# Docker daemon. Same topology as docker-compose.yml, with processes instead of
# containers, because a Pod cannot run compose -- see docs/DEPLOYMENT.md §4.
#
#     cd /workspace/copilot && bash deploy/pod-bootstrap.sh
#
# ── Read this before running it ───────────────────────────────────────────────
#
# This is the fallback path, and the one part of the deployment that has not been
# run against real hardware. It assumes a Debian/Ubuntu pod image with apt and
# root, which is what RunPod's PyTorch templates give you, and it will need
# adjusting for an image that differs. Every step announces itself so a failure
# names its own step.
#
# Idempotent by design: re-running it after a fix should be safe, and it skips
# work that is already done rather than repeating it.
#
# The compose path on a real VM is better in every way. Prefer it if you can.

set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Everything persistent goes under one root, because on RunPod only /workspace
# survives a pod restart. Postgres data and Ollama weights both live here, which
# is the difference between a restart costing seconds and costing a 5.7GB
# re-download plus a re-ingest.
WORKSPACE="${WORKSPACE:-/workspace}"
PGDATA="${PGDATA:-$WORKSPACE/pgdata}"
OLLAMA_HOME="${OLLAMA_HOME:-$WORKSPACE/ollama}"
LOGS="${LOGS:-$WORKSPACE/logs}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AGENT_MODEL="${AGENT_MODEL:-qwen2.5:7b}"
AGENT_ROUTER_MODEL="${AGENT_ROUTER_MODEL:-qwen2.5:1.5b}"
TICKERS="${TICKERS:-NVDA AAPL}"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

mkdir -p "$LOGS" "$OLLAMA_HOME"

# ─────────────────────────────────────────────────────────────────────────────
step "0. What we are running on"
# ─────────────────────────────────────────────────────────────────────────────

# nvidia-smi failing here is fatal and worth failing on immediately: the entire
# reason to be on this host is the GPU, and everything below would otherwise
# succeed and run on CPU.
if ! have nvidia-smi || ! nvidia-smi >/dev/null 2>&1; then
  echo "FATAL: no usable GPU visible (nvidia-smi failed)." >&2
  echo "  Nothing below would fail -- Ollama would silently run on CPU." >&2
  exit 1
fi
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
echo "workspace: $WORKSPACE   repo: $REPO"

# ─────────────────────────────────────────────────────────────────────────────
step "1. System packages"
# ─────────────────────────────────────────────────────────────────────────────

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# postgresql-16-pgvector is what turns filing search from a sequential scan into
# an HNSW index lookup. It is not in Ubuntu's own archive, so this adds PGDG --
# the same reason docker-compose.yml uses the pgvector image rather than
# postgres:alpine. Migration 0002 probes for the extension and silently falls
# back to a float array column without it.
if [ ! -f /etc/apt/sources.list.d/pgdg.list ]; then
  apt-get install -y -qq curl ca-certificates gnupg lsb-release
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
fi

apt-get install -y -qq \
  postgresql-16 postgresql-16-pgvector \
  redis-server \
  git curl build-essential

# Node for the frontend. The pod images ship Python, not Node.
if ! have node || [ "$(node --version | cut -c2- | cut -d. -f1)" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -qq nodejs
fi
echo "postgres: $(pg_config --version)   node: $(node --version)"

# ─────────────────────────────────────────────────────────────────────────────
step "2. Postgres, with its data on the persistent volume"
# ─────────────────────────────────────────────────────────────────────────────

PGBIN="/usr/lib/postgresql/16/bin"

if [ ! -f "$PGDATA/PG_VERSION" ]; then
  mkdir -p "$PGDATA"
  chown -R postgres:postgres "$PGDATA"
  su postgres -c "$PGBIN/initdb -D $PGDATA"
fi
chown -R postgres:postgres "$PGDATA"

# Only start it if it is not already running. `pg_ctl start` against a live
# cluster exits non-zero, which under `set -e` would abort the whole script on
# the second run -- and re-running this after fixing one step is the normal case.
#
# Loopback only. The pod's exposed ports are reachable over RunPod's public
# proxy, and a database listening on 0.0.0.0 with a trivial password is not
# something to leave to chance.
if su postgres -c "$PGBIN/pg_ctl -D $PGDATA status" >/dev/null 2>&1; then
  echo "  already running"
else
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -l $LOGS/postgres.log \
    -o \"-c listen_addresses=127.0.0.1 -p 5432\" start" || {
      echo "Postgres failed to start; last 20 lines of $LOGS/postgres.log:" >&2
      tail -20 "$LOGS/postgres.log" >&2; exit 1; }
fi

# Wait for it rather than sleeping and hoping.
for _ in $(seq 30); do
  su postgres -c "$PGBIN/pg_isready -h 127.0.0.1 -q" && break
  sleep 1
done
su postgres -c "$PGBIN/pg_isready -h 127.0.0.1" || { echo "Postgres never became ready" >&2; exit 1; }

su postgres -c "psql -h 127.0.0.1 -tAc \
  \"SELECT 1 FROM pg_database WHERE datname='finance_copilot'\"" | grep -q 1 \
  || su postgres -c "createdb -h 127.0.0.1 finance_copilot"

# Proves the extension is installable before the migration depends on it, so a
# missing package fails here with a clear message instead of silently degrading
# retrieval to a sequential scan.
su postgres -c "psql -h 127.0.0.1 -d finance_copilot -c \
  'CREATE EXTENSION IF NOT EXISTS vector'" \
  || { echo "FATAL: pgvector not installable -- retrieval would fall back to a scan" >&2; exit 1; }

# ─────────────────────────────────────────────────────────────────────────────
step "3. Redis"
# ─────────────────────────────────────────────────────────────────────────────

# Matches docker-compose.yml: no persistence. Rate-limit counters are worth less
# than the window they cover, and fsyncing them would put a disk write in front
# of every request.
if ! redis-cli ping >/dev/null 2>&1; then
  redis-server --daemonize yes --bind 127.0.0.1 --port 6379 \
    --save '' --appendonly no \
    --maxmemory 256mb --maxmemory-policy allkeys-lru \
    --logfile "$LOGS/redis.log"
fi
redis-cli ping

# ─────────────────────────────────────────────────────────────────────────────
step "4. Ollama"
# ─────────────────────────────────────────────────────────────────────────────

have ollama || curl -fsSL https://ollama.com/install.sh | sh

# These have to be in the environment of the process that STARTS the server, not
# merely exported somewhere. That exact mistake is recorded in docs/INFERENCE.md:
# OLLAMA_CONTEXT_LENGTH was set, the server reported the old value, and 3,342
# tokens of evidence were being discarded per request with no error.
#
# Values are the GPU ones from docker-compose.gpu.yml. See that file for why.
export OLLAMA_MODELS="$OLLAMA_HOME/models"
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-16384}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-2}"
export OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-2}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:--1}"
export OLLAMA_HOST="127.0.0.1:11434"

if ! curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  nohup ollama serve > "$LOGS/ollama.log" 2>&1 &
  for _ in $(seq 60); do
    curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break
    sleep 1
  done
fi
curl -sf http://127.0.0.1:11434/api/version || { echo "Ollama never came up" >&2; exit 1; }

ollama pull "$AGENT_MODEL"
ollama pull "$AGENT_ROUTER_MODEL"

# The check that the whole GPU story rests on. A pod with a visible GPU can
# still end up on CPU if the model does not fit in VRAM, and the only symptom is
# that everything works slowly.
ollama run "$AGENT_MODEL" "hi" >/dev/null 2>&1 || true
echo "--- ollama ps (PROCESSOR must say GPU) ---"
ollama ps

# ─────────────────────────────────────────────────────────────────────────────
step "5. Python dependencies"
# ─────────────────────────────────────────────────────────────────────────────

cd "$REPO"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

# CPU torch, on a GPU host, deliberately. The only torch workload here is a
# bge-small forward pass (33M parameters); the default wheel pulls ~2.5GB of
# CUDA to do arithmetic that finishes in milliseconds on a CPU, and the GPU is
# for the model generating tokens. Same reasoning as the backend Dockerfile.
pip install -q --upgrade pip
pip install -q torch --index-url https://download.pytorch.org/whl/cpu
pip install -q -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
step "6. Configuration"
# ─────────────────────────────────────────────────────────────────────────────

# Unlike the compose path -- where topology lives in `environment:` and never
# touches .env -- there is no compose file here, so the service URLs have to be
# written into .env. They are all loopback, because every process is in this one
# container.
if [ ! -f .env ]; then
  cp .env.example .env
  python3 - <<'PY'
import re, secrets, pathlib
env = pathlib.Path(".env")
text = env.read_text(encoding="utf-8")
settings = {
    "JWT_SECRET": secrets.token_urlsafe(48),
    "DATABASE_URL": "postgresql+asyncpg://postgres@127.0.0.1:5432/finance_copilot",
    "REDIS_URL": "redis://127.0.0.1:6379/0",
    "INFERENCE_BASE_URL": "http://127.0.0.1:11434/v1",
    "AGENT_INFERENCE_BASE_URL": "http://127.0.0.1:8000/v1",
    # On GPU: a 300s read timeout stops being a slow answer and becomes a hung
    # one. See docker-compose.gpu.yml.
    "INFERENCE_READ_TIMEOUT": "90",
}
for key, value in settings.items():
    line = f"{key}={value}"
    text, n = re.subn(rf"(?m)^{key}=.*$", line, text)
    if not n:
        text += f"\n{line}\n"
env.write_text(text, encoding="utf-8")
print("  wrote .env:", ", ".join(settings))
PY
  echo
  echo "  !! STILL TO DO BY HAND in .env -- neither can be guessed from here:"
  echo "     SEC_EDGAR_USER_AGENT   a real contact address, or EDGAR refuses"
  echo "     CORS_ORIGINS           https://<pod-id>-3000.proxy.runpod.net"
  echo
fi

# ─────────────────────────────────────────────────────────────────────────────
step "7. Schema and filing index"
# ─────────────────────────────────────────────────────────────────────────────

python -m alembic upgrade head

# Skipped if chunks are already indexed: embedding is the slow stage by a wide
# margin and a re-run of this script should not pay for it twice.
#
# The exit code carries the answer rather than parsed stdout -- `stats()` returns
# `total_chunks`, and grepping its output for a "0" is the kind of check that
# silently inverts when a key is renamed.
already_indexed() {
  python - <<'PY'
import asyncio, sys
from rag.vector_store import connection, stats

async def main() -> int:
    async with connection() as conn:
        indexed = (await stats(conn))["total_chunks"]
    print(f"  {indexed} chunk(s) indexed")
    return 0 if indexed else 1

sys.exit(asyncio.run(main()))
PY
}

if already_indexed; then
  echo "  skipping ingest (delete the chunks to force a rebuild)"
else
  # shellcheck disable=SC2086 -- TICKERS is deliberately word-split
  python scripts/index_filings.py $TICKERS
fi

# ─────────────────────────────────────────────────────────────────────────────
step "8. The API and the frontend"
# ─────────────────────────────────────────────────────────────────────────────

nohup uvicorn api.main:app --host 0.0.0.0 --port 8000 > "$LOGS/api.log" 2>&1 &
for _ in $(seq 30); do
  curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1 && break
  sleep 1
done
curl -sf http://127.0.0.1:8000/health || { echo "API never came up; see $LOGS/api.log" >&2; exit 1; }

# NEXT_PUBLIC_API_BASE is compiled into the client bundle by `npm run build`, so
# it must be set BEFORE the build and must be a URL the visitor's browser can
# resolve. Left unset, the frontend renders perfectly and sends every request to
# the visitor's own 127.0.0.1. See frontend/Dockerfile for the long version.
cd "$REPO/frontend"
if [ -z "${PUBLIC_API_BASE:-}" ]; then
  echo
  echo "  !! PUBLIC_API_BASE is unset, so the frontend will be built pointing at"
  echo "     127.0.0.1:8000 -- which means the visitor's own machine, not this pod."
  echo "     Re-run with:  PUBLIC_API_BASE=https://<pod-id>-8000.proxy.runpod.net \\"
  echo "                   bash deploy/pod-bootstrap.sh"
  echo
fi
npm ci
NEXT_PUBLIC_API_BASE="${PUBLIC_API_BASE:-http://127.0.0.1:8000}" npm run build
# -H 0.0.0.0 is required: the default binds localhost, and the pod's port proxy
# connects from outside the process's own loopback.
nohup npm run start -- -H 0.0.0.0 -p 3000 > "$LOGS/frontend.log" 2>&1 &

# ─────────────────────────────────────────────────────────────────────────────
step "Done"
# ─────────────────────────────────────────────────────────────────────────────

cat <<EOF

  processes   postgres, redis, ollama, uvicorn :8000, next :3000
  logs        $LOGS/{postgres,redis,ollama,api,frontend}.log

  verify, in this order -- each check isolates one layer:

    ollama ps                                  # PROCESSOR must say GPU
    curl -s localhost:8000/health              # the process
    curl -s localhost:8000/health/db           # Postgres + migrations
    curl -s localhost:8000/health/redis        # note "limiting": true
    curl -s localhost:8000/v1/models           # the models actually loaded

  then open  https://<pod-id>-3000.proxy.runpod.net

  if the page renders but every request fails, it is PUBLIC_API_BASE or
  CORS_ORIGINS -- see docs/DEPLOYMENT.md section 6.
EOF
