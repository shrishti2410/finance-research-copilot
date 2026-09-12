# Backend: FastAPI app, agent loop, ingestion pipeline, eval harness.
#
#   docker build -t copilot-backend .
#
# One image serves three roles, because they share the same code and the same
# dependency set and splitting them would mean maintaining three:
#
#   the API server         uvicorn api.main:app          (the CMD)
#   one-shot admin tasks   alembic upgrade head, scripts/index_filings.py
#   the eval harness       python -m eval.run_eval
#
# Deliberately CPU-only, on a GPU host. See the torch install below.

FROM python:3.11-slim AS base

# PYTHONUNBUFFERED so logs reach `docker logs` as they happen rather than when a
# 4KB buffer fills -- the difference between watching a slow ingest and staring
# at nothing. PIP_NO_CACHE_DIR because a cache in a layer is dead weight.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ─────────────────────────────────────────────────────────────────────────────
# Dependencies, in their own layer so a code change does not reinstall torch.
# ─────────────────────────────────────────────────────────────────────────────

COPY requirements.txt ./

# torch first, explicitly from the CPU index.
#
# This is the one counter-intuitive line in the file: the whole point of the
# deployment is a GPU, and the backend deliberately does not use it. The only
# torch workload here is a bge-small forward pass (33M parameters, 384
# dimensions) to embed chunks and queries. The default Linux wheel bundles CUDA
# and pulls ~2.5GB of nvidia-* packages to do arithmetic that finishes in
# milliseconds on a CPU. The GPU is for the model that generates tokens at
# 3 tok/s on a CPU, which is the actual bottleneck -- see docs/INFERENCE.md.
#
# Kept in sync with the note in requirements.txt, which says the same thing.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

RUN pip install -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# Model weights, baked in.
#
# Two HuggingFace downloads happen lazily on first use: the bge-small encoder
# (rag/embeddings.py) and the Qwen tokenizer used to size chunks
# (ingestion/chunker.py). Left to run time they turn the first request into a
# silent multi-hundred-megabyte download -- which looks exactly like a hang, and
# fails outright on a host with no egress to huggingface.co.
#
# Fetching them at build time makes the image self-contained: the container
# needs no HuggingFace access to serve a request.
# ─────────────────────────────────────────────────────────────────────────────

ENV HF_HOME=/opt/hf

RUN python -c "\
from transformers import AutoModel, AutoTokenizer;\
AutoTokenizer.from_pretrained('BAAI/bge-small-en-v1.5');\
AutoModel.from_pretrained('BAAI/bge-small-en-v1.5');\
AutoTokenizer.from_pretrained('Qwen/Qwen2.5-1.5B-Instruct');\
print('model cache warm')"

# ─────────────────────────────────────────────────────────────────────────────
# Application code
# ─────────────────────────────────────────────────────────────────────────────

COPY alembic.ini pyproject.toml ./
COPY migrations/ ./migrations/
COPY agent/ ./agent/
COPY api/ ./api/
COPY auth/ ./auth/
COPY core/ ./core/
COPY db/ ./db/
COPY eval/ ./eval/
COPY ingestion/ ./ingestion/
COPY rag/ ./rag/
COPY scripts/ ./scripts/
COPY tools/ ./tools/
COPY tests/ ./tests/

# The EDGAR cache lives here and is a mounted volume in compose. Created (and
# owned) before dropping privileges, or the unprivileged user cannot write the
# first filing it downloads.
RUN mkdir -p /app/data/edgar

# Non-root. This process accepts requests from the internet and runs an agent
# loop that fetches and parses untrusted HTML from EDGAR; root inside the
# container is one kernel bug away from root on the host.
RUN useradd --create-home --uid 10001 copilot \
    && chown -R copilot:copilot /app/data
USER copilot

EXPOSE 8000

# Liveness only, matching what /health promises: it deliberately does not touch
# Postgres or Ollama, so a cold model load cannot get the API container
# restarted. Readiness lives at /health/db and /v1/health -- see api/main.py.
#
# urllib rather than curl, so the image needs no extra package to be monitorable.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys;\
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

# No --reload: it watches the filesystem and doubles memory for nothing here.
# One worker, deliberately. The agent loop is bounded by the inference server's
# own throughput, and a second worker would multiply the HF model in memory
# while contending for the same single GPU behind Ollama.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
