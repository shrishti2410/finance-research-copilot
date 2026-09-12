# Deploying to a cloud GPU host

Everything here targets **one GPU VM running the whole stack**: Postgres, Redis,
Ollama, the API and the frontend on a single machine, over a single Docker
network. That is the right shape for this project — one agent loop against one
model, where the model is the entire cost — and it keeps the deployment to one
command sequence.

| | |
|---|---|
| **Recommended target** | Any host where you have Docker and root: Lambda Labs on-demand, RunPod **Bare Metal**, a GCP/AWS GPU VM. |
| **Not usable as-is** | RunPod **Pods**. They are containers with no Docker daemon, so `docker compose` cannot run inside one. [See below](#runpod-pods-why-compose-does-not-work-there). |
| **Minimum GPU** | 16GB VRAM holds `qwen2.5:7b` at Q4_K_M (~4.7GB) plus the 1.5b router (~1GB) with room for KV cache. A 24GB card (A10G, L4, 3090, 4090) is comfortable. |
| **Disk** | ~25GB: ~6GB of model weights, ~8GB of images, the rest Postgres and the EDGAR cache. |

---

## 0. What has actually been run, and what has not

Stated first, and in this much detail, because the difference between "this
config is correct" and "this config has been executed" is exactly what a reader
needs and exactly what deployment documentation tends to blur.

| | status |
|---|---|
| `docker-compose.yml` — the CPU stack, all five services | see [§8](#8-what-the-cpu-run-proved) |
| `Dockerfile`, `frontend/Dockerfile` | built and run as part of that |
| `docker-compose.gpu.yml` — the GPU overlay | **written and config-validated; never executed** |
| `deploy/pod-bootstrap.sh` — the RunPod Pod fallback | **written; never executed** |
| Provider steps in [§3](#3-deploying-to-lambda-labs-the-recommended-path) / [§4](#4-runpod) | from provider documentation, not from an account |

### Why the GPU path was not run

Renting a GPU is the only way to execute it, and it costs money per hour for a
result that is largely predictable from the CPU run. The decision was to prove
the parts that can be proven for free — the images, the service topology, the
inter-service networking, the migration and model-pull sequence, the app
behaving identically in containers — and to leave exactly one variable unproven:
whether the GPU reservation block grants the device.

That is an honest trade, not a hidden gap, so here is precisely what remains
unverified and what it would take to close it.

### What "config-validated" means for the GPU overlay

Checked mechanically:

- Both compose files parse as YAML.
- Every service the overlay names (`ollama`, `backend`) exists in the base file,
  so the overlay adds to real services rather than silently creating stubs.
- Every service referenced by a command in this document exists.
- The overlay's `deploy.resources.reservations.devices` block matches the shape
  in Docker's own GPU documentation, and the same block is already in
  `docker-compose.vllm.yml`.

Not checked, because it needs the hardware:

- That `driver: nvidia, count: all` actually passes the device through on a
  given host. This depends on the NVIDIA Container Toolkit being installed and
  registered with the Docker daemon — a host property, not a property of this
  file.
- That `qwen2.5:7b` plus the 1.5b router fit in a particular card's VRAM at
  `OLLAMA_CONTEXT_LENGTH=16384` and `OLLAMA_NUM_PARALLEL=2`. Those two numbers
  multiply into VRAM and are the first things to lower if `ollama ps` reports
  anything other than `100% GPU`.
- Every latency figure implied by moving to a GPU. Nothing in this repo has
  measured this app on a GPU; the CPU numbers in `docs/INFERENCE.md` are the only
  measured ones, and `INFERENCE_READ_TIMEOUT=90` in the overlay is a judgement
  from them rather than an observation.

### The failure mode to expect first

If the device is not passed through, **nothing errors**. Ollama probes for a
GPU, finds none, and serves on CPU — a healthy server, several times slower than
the hardware being paid for. This is why [§3](#verify-in-this-order) makes
`ollama ps` reporting `100% GPU` the first check after `up`, ahead of any
application check.

---

## 1. Should Ollama be a container, or installed on the host?

**Run it as a container.** The compose file already does.

The decisive reason is specific to this project rather than general good
practice. `docs/INFERENCE.md` records a bug that cost real time: setting
`OLLAMA_CONTEXT_LENGTH` had no effect, because the value has to be in the
environment of the process that *starts* Ollama, and the tray app had been
launched from a shell that predated the change. The server reported the old
context length while the config said otherwise, and the app silently discarded
3,342 tokens of evidence per request.

A host install puts that class of bug back on the table on every redeploy. In a
container the tuning is declarative — it is in `docker-compose.yml`, it is in
version control, and `docker compose up` cannot pick up a stale value from a
shell that no longer exists.

The rest:

- **One artifact.** `docker compose up -d` brings up inference *with* the app.
  No separate host-level install whose version differs per provider image.
- **Ollama has no authentication.** As a container its port is published on
  loopback only and the backend reaches it at `http://ollama:11434` over the
  compose network. A host install listening on `0.0.0.0:11434` on a machine with
  a public IP is an open inference endpoint on your bill, and `/api/pull` lets a
  stranger fill your disk.
- **Weights survive restarts** in a named volume, so `up`/`down` does not
  re-download ~5.7GB.
- **GPU passthrough is six declarative lines** and identical on every provider.

The overhead is a device passthrough, not virtualisation — the container talks to
the same driver. There is no measurable inference penalty.

**Run it on the host instead** in exactly two cases: the host has no Docker
daemon (a RunPod Pod), or you want one Ollama shared by several deployments.

---

## 2. What changes between CPU and GPU

`docker-compose.gpu.yml` **is** the diff. It is an overlay containing nothing but
the difference, so it can be read as the answer to this question:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

### Does Ollama auto-detect CUDA?

**Yes — and that alone gets you nothing.**

The `ollama/ollama` image ships CUDA runtime libraries and probes for a usable
GPU at start-up. There is no flag, no env var, no config file entry to turn it
on; given a visible device it uses it, given none it runs on CPU. Same image,
same tag, both cases.

What is missing on a GPU host is **visibility**. Docker exposes no host devices
to a container by default, so Ollama's probe finds nothing and comes up as a
perfectly healthy CPU server. That is why this is worth being careful about: the
failure mode is not an error, it is everything working and being several times
slower than the hardware you are paying for.

So the GPU is granted by Docker, not requested by Ollama:

```yaml
# docker-compose.gpu.yml
services:
  ollama:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

This is compose's spelling of `docker run --gpus=all`, and it requires the
**NVIDIA Container Toolkit** installed and registered with the Docker daemon on
the host. On Lambda Labs on-demand instances it is preinstalled; on a bare
Ubuntu VM you install it yourself (step 3 below).

### The settings that change, and why

| setting | CPU (measured) | GPU | why it moves |
|---|---|---|---|
| `deploy.resources.reservations.devices` | absent | `driver: nvidia, count: all` | **Required.** Without it Ollama runs on CPU and says nothing. |
| `OLLAMA_NUM_PARALLEL` | `1` | `2` | The KV cache is allocated per slot. On the dev host free RAM dipped to 0.08GB with both models loading, so extra slots were unaffordable. With VRAM to spare a second slot lets a request proceed instead of queueing behind a streaming answer. |
| `OLLAMA_CONTEXT_LENGTH` | `8192` | `16384` | 8192 was the smallest power of two clearing the largest observed prompt (~5,852 tokens) plus the 512-token generation cap. 16384 is headroom for longer conversations, not a fix for a known truncation. Note it multiplies with `NUM_PARALLEL`. |
| `OLLAMA_KEEP_ALIVE` | default (5m) | `-1` | Keeps both models resident. A reload cost **172–207s** on CPU; cheaper on GPU but still the largest latency spike a user can hit, and pointless to pay on a dedicated host. |
| `OLLAMA_MAX_LOADED_MODELS` | `2` | `2` | Unchanged. The router split alternates between two models inside one turn, and an eviction between them is the worst case. |
| `INFERENCE_READ_TIMEOUT` | `300` | `90` | 300s existed because CPU generated at 2.8–4.2 tok/s and a long answer legitimately took minutes. On a GPU, 300s is no longer a slow answer — it is a hung one, and waiting five minutes to find out is worse than failing in ninety seconds. |

Two things deliberately **not** changed, both documented in the overlay:

- **`AGENT_ROUTER_MODEL`** stays `qwen2.5:1.5b`. The 1.5b/7b split was worth
  −39%/−61%/−13% on three representative questions *on CPU*, where generation was
  ~97% of latency. On a GPU the saving shrinks while the cost does not: the small
  model measurably fails to recover from a bad tool call, which is why the loop
  escalates after any failed one. Disabling it (`AGENT_ROUTER_MODEL=""`) may well
  be right — that is a measurement, and `python -m eval.compare` answers it in
  one run. It is left at the value that has actually been measured.
- **`AGENT_MAX_TOKENS`** stays 512. It was chosen to clear the longest observed
  answer (~368 tokens), not to save CPU time. Faster generation is not a reason
  to let answers run longer.

### A note on vLLM

`docker-compose.vllm.yml` predates this and serves the same OpenAI-compatible API
from vLLM instead. On a GPU, vLLM is the better server for concurrent load —
continuous batching is its whole point, where Ollama queues. It is not the
default here for one reason: every latency and accuracy number this project has
was measured against Ollama, and swapping the inference engine invalidates all of
them at once. If you want it, point `INFERENCE_BASE_URL` at it and re-run
`python -m eval.compare` before believing anything.

### One more thing the GPU host changes: pgvector

`docker-compose.yml` uses `pgvector/pgvector:pg16`, not `postgres:16-alpine`.

This is a functional upgrade, not a preference. Migration 0002 probes for the
`vector` extension and, when it is missing, falls back to a float array column
with no ANN index — correct results, sequential scan on every search. The dev
host has no pgvector, so the project has been running on that fallback the whole
time. On the deployment image, the same migration creates `vector(384)` with an
HNSW index.

**This only happens on a fresh volume.** An existing `pgdata` initialised by the
alpine image does not gain the extension when you swap the image; it has to be
there before migration 0002 runs. On a new cloud host that is automatic.

---

## 3. Deploying to Lambda Labs (the recommended path)

Lambda on-demand instances ship Docker **and** the NVIDIA Container Toolkit
preinstalled, which is why this is the shortest route.

### Provision

1. Create an account at [lambda.ai](https://lambda.ai) and add a payment method.
2. **SSH keys → Add SSH key**, paste your public key. Do this first; you cannot
   add one to a running instance without the console.
3. **Instances → Launch instance.** Pick a single-GPU type with ≥16GB VRAM
   (A10 24GB or L4 is plenty; an A100 is wasted on a 7B model). Ubuntu, and at
   least 100GB of storage.
4. Note the public IP. Everything below calls it `$HOST`.

```bash
ssh ubuntu@$HOST
nvidia-smi          # the driver is live and a GPU is listed
docker --version    # preinstalled on Lambda on-demand
```

If `docker` is missing (a bare VM on another provider), install it and the
toolkit:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# NVIDIA Container Toolkit: the piece that lets a container see the GPU
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# The check that matters: a container can see the GPU
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

### Deploy

```bash
git clone <your-repo-url> copilot && cd copilot

# 1. Configuration. Start from .env.example, then apply the deployment delta.
cp .env.example .env
cat .env.deploy.example        # the four or five lines that must change

# JWT_SECRET: compose refuses to start without it
python3 -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(48))" >> .env

# The two that depend on the host's address. Both matter -- see step 5.
echo "PUBLIC_API_BASE=http://$HOST:8000"                      >> .env
echo "CORS_ORIGINS=http://$HOST:3000"                         >> .env
echo "SEC_EDGAR_USER_AGENT=finance-research-copilot/0.1 (you@yourdomain.com)" >> .env

# 2. Build and start. First build is ~10 minutes: torch, transformers, and the
#    bge-small + Qwen tokenizer weights are baked into the backend image.
export COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
docker compose build
docker compose up -d

# 3. Schema. Once, explicitly -- not on every container start.
docker compose run --rm migrate

# 4. Model weights, ~5.7GB into the ollama volume. Once.
docker compose run --rm model-pull

# 5. Filings, so search_filings has an index to search. Minutes, mostly
#    embedding on CPU. Add whichever tickers you care about.
docker compose run --rm backend python scripts/index_filings.py NVDA AAPL
```

`export COMPOSE_FILE=...` means every later `docker compose` command in that
shell carries both files. Forget it and you get a CPU deployment that works
perfectly and runs several times slower.

### Verify, in this order

Each check isolates one layer, so a failure names the layer.

```bash
# The GPU actually reached Ollama. THE check -- see §2.
docker compose exec ollama nvidia-smi
docker compose exec ollama ollama ps        # PROCESSOR must say "100% GPU"

# Ollama passing nvidia-smi but reporting "100% CPU" means the device is
# visible and the model did not fit in VRAM. Lower OLLAMA_NUM_PARALLEL or
# OLLAMA_CONTEXT_LENGTH before suspecting anything else.

# The app's own readiness, one dependency at a time
curl -s localhost:8000/health          # {"status":"ok"} -- process only
curl -s localhost:8000/health/db       # {"status":"ok"} -- Postgres + migrations
curl -s localhost:8000/health/redis    # note "limiting": true
curl -s localhost:8000/v1/health       # the inference upstream
curl -s localhost:8000/v1/models       # the models model-pull fetched

# End to end, as a user. ~10-30s on a GPU.
curl -s -X POST localhost:8000/auth/signup \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"a-real-password"}'
# /ask requires a conversation_id, so there are two steps rather than one.
# scripts/verify_deployment.py does this whole flow and checks the result --
# prefer it to hand-rolled curl:
python scripts/verify_deployment.py --base-url http://$HOST:8000

# The suite, in the deployed image. 877 tests.
docker compose run --rm backend python -m pytest -q
# Safety properties alone -- these are release blockers, not flaky assertions:
docker compose run --rm backend python -m pytest -m safety -q
```

Then open `http://$HOST:3000` in a browser and sign in. If the page renders but
every request fails, it is one of the two host-address settings — see
[§6](#6-when-it-is-not-localhost-any-more).

### Firewall

Lambda instances come with a public IP and no firewall. Postgres, Redis and
Ollama are published on `127.0.0.1` by the compose file and are not exposed. The
two that are, deliberately, are 3000 and 8000. To restrict them to yourself:

```bash
sudo ufw allow 22/tcp
sudo ufw allow from <your-ip> to any port 3000 proto tcp
sudo ufw allow from <your-ip> to any port 8000 proto tcp
sudo ufw --force enable
```

---

## 4. RunPod

### RunPod Pods: why compose does not work there

A RunPod Pod *is* a container. There is no Docker daemon inside it and
Docker-in-Docker is not supported, so `docker compose up` fails with "cannot
connect to the Docker daemon" no matter what you install. This is a platform
property, not a missing package.

Two ways forward.

### Option A — RunPod Bare Metal (use the compose path)

Bare Metal gives root on the machine, so it behaves like any other VM: follow
[§3](#3-deploying-to-lambda-labs-the-recommended-path) exactly, including the
Docker and NVIDIA Container Toolkit install, since a bare host has neither.

This is the option to pick if you want RunPod specifically.

### Option B — a Pod, without Docker

The Pod already is a GPU container, so the services run in it directly. This is
what `deploy/pod-bootstrap.sh` does; it is the same topology as the compose file
with processes instead of containers.

```bash
# Pod → Deploy, a ≥16GB-VRAM GPU, template "RunPod PyTorch" (Ubuntu + CUDA).
# Expose HTTP ports 3000 and 8000. Give /workspace at least 60GB: it is the
# only persistent disk, and model weights go there.

# In the pod's web terminal or over SSH:
cd /workspace
git clone <your-repo-url> copilot && cd copilot
bash deploy/pod-bootstrap.sh
```

Read the script before running it. It installs Postgres (with pgvector), Redis,
Node and Ollama, then starts all five processes — and unlike the compose path, it
is the part of this document that is most likely to need a small fix for
whichever base image your pod happens to have.

**Ports and URLs.** RunPod gives each exposed port its own hostname:
`https://<pod-id>-8000.proxy.runpod.net`. Those are the values for
`PUBLIC_API_BASE` and `CORS_ORIGINS`, and the frontend must be rebuilt if the
pod id changes — which it does every time you recreate the pod. The proxy is
HTTPS, so both URLs are `https://`, and mixing an `https://` frontend with an
`http://` API base gives you a browser mixed-content block rather than a
network error.

**Do not expose 11434.** The proxy URL is unauthenticated. An exposed Ollama port
is an open inference endpoint anyone with the URL can use and `/api/pull` into.

---

## 5. Costs, and turning it off

Per-hour GPU billing runs whether anyone is using the app.

```bash
docker compose down             # stop, keep the volumes
docker compose down -v          # also delete Postgres, weights, EDGAR cache
```

`down` alone is what you want between sessions: the volumes survive, so a later
`up -d` skips the migration, the model pull and the filing ingest. Then
**terminate the instance in the provider console** — a stopped container on a
running GPU instance still bills. On RunPod Pods, `/workspace` persists while the
pod exists; terminating it takes the weights with it.

---

## 6. When it is not localhost any more

Four settings, and each one fails in its own way.

### `PUBLIC_API_BASE` — compiled into the frontend, not read at run time

`src/lib/api.ts` reads `process.env.NEXT_PUBLIC_API_BASE`. Every page in this app
is `"use client"`, so every request is made *by the browser*. Next replaces
`NEXT_PUBLIC_*` with a literal string during `next build`, which means:

- It must be a URL the **visitor's browser** resolves. Never
  `http://backend:8000` — that name exists only on the compose network.
- Setting it in compose `environment:` does nothing. It is already compiled in.
  Changing it requires `docker compose build frontend`.

Get this wrong and the frontend loads, renders, routes, and sends every request
to the *visitor's own* machine. Nothing appears in the server logs, because
nothing reaches the server.

**So the build refuses to produce that.** `next.config.ts` asserts the value
during `next build` — before it compiles anything — and throws if it is unset,
not an `http(s)` URL, one of this stack's compose service names, or a loopback
address:

```
NEXT_PUBLIC_API_BASE points at 127.0.0.1, which in a browser means the
visitor's own machine.
  ...
  Building a container to run on this machine? Say so explicitly:
    ALLOW_LOCALHOST_API_BASE=1 ...
```

There is no default value anywhere in the chain — not in the Dockerfile `ARG`,
not in the compose `args` — because the only plausible default is a localhost
one, and a localhost default is precisely the failure. Unset reaches the
assertion as empty and stops the build.

To build a stack you intend to browse from the host running it, opt in:

```bash
ALLOW_LOCALHOST_API_BASE=1 docker compose build frontend
# or set PUBLIC_API_BASE + ALLOW_LOCALHOST_API_BASE in .env
```

`next dev` is never checked; localhost is correct there.

One case the build cannot catch: an image built legitimately for localhost and
then served from somewhere else. Only the browser knows, so `src/lib/api.ts`
checks at request time and replaces the generic "cannot reach the API" with the
actual reason — that the page is served from one host and was compiled to call
another.

### `CORS_ORIGINS` — an allow-list, never `*`

The API allows a fixed list of origins because these requests carry a bearer
token, and a wildcard origin on a credentialed API is how one site reads
another's data. An origin missing from the list produces a CORS failure visible
only in the browser console; the server sees a normal preflight.

Exact string match, scheme and port included: `http://203.0.113.10:3000` is not
`https://203.0.113.10:3000` and is not `http://203.0.113.10`.

### Service URLs — already handled, do not touch

`DATABASE_URL`, `REDIS_URL` and `INFERENCE_BASE_URL` all point at `127.0.0.1` in
`.env`, which is correct when you run from a shell and wrong inside a container
in the most confusing way available: the address resolves, connects to nothing,
and the error names a port that is genuinely in use on the host.

`docker-compose.yml` sets all three in `environment:`, which takes precedence
over `env_file`, so the container gets `postgres:5432`, `redis:6379` and
`ollama:11434` from Docker's DNS regardless of what `.env` says. The split is
deliberate: secrets and policy live in `.env`, topology lives in the compose
file, and you never edit `.env` to deploy.

One exception that looks like a mistake and is not:

```yaml
AGENT_INFERENCE_BASE_URL: http://127.0.0.1:8000/v1
```

The agent calls the app's **own** OpenAI-compatible proxy, so its inference goes
through the same timeouts, logging and upstream config as every other client.
Inside the backend container `127.0.0.1:8000` is that process. `http://backend:8000`
would also work and would add a pointless hop through the Docker bridge.

### `INTERNAL_TOKEN` — leave it unset

It lets the process recognise its own loopback calls to `/v1` and skip rate
limiting on them. It is generated randomly per process, which is correct while
the agent runs inside the API process — as it does here. Set it explicitly only
if you split them, or a single `/ask` spends five of the caller's own requests on
the app talking to itself.

---

## 7. Reference: the whole sequence

On a host with Docker, the toolkit, and the repo cloned:

```bash
cp .env.example .env
python3 -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(48))" >> .env
echo "PUBLIC_API_BASE=http://$HOST:8000" >> .env
echo "CORS_ORIGINS=http://$HOST:3000"    >> .env
echo "SEC_EDGAR_USER_AGENT=finance-research-copilot/0.1 (you@yourdomain.com)" >> .env

export COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
docker compose build
docker compose up -d
docker compose run --rm migrate
docker compose run --rm model-pull
docker compose run --rm backend python scripts/index_filings.py NVDA AAPL

docker compose exec ollama ollama ps     # must say 100% GPU
curl -s localhost:8000/health/db
open http://$HOST:3000
```

Nine commands. Of the two that used to be easy to get wrong, `PUBLIC_API_BASE`
now stops the build rather than shipping a broken client, and `CORS_ORIGINS`
fails visibly in the browser console. That leaves `COMPOSE_FILE` as the only
silent one: omit it and you deploy on CPU, with everything working and nothing
saying so.

---

## 8. What the CPU run proved

The GPU is the only part of this that costs money, and it is one variable. So the
CPU stack — the same five services, the same images, the same networking, the
same startup sequence, minus the device reservation — is run locally instead, and
that is what turns "the config is correct" into "the system works".

### Running it yourself

This host is Windows 11 Home with no Docker. The install needs an administrator
and a reboot:

```powershell
# 1. WSL2, which Docker Desktop uses as its backend. Elevated prompt, then reboot.
wsl --install

# 2. Docker Desktop
winget install -e --id Docker.DockerDesktop

# 3. Start Docker Desktop once from the Start menu so it initialises the WSL
#    backend, then confirm from any shell:
docker version
docker compose version
```

**Free three ports first.** The dev processes from working on this app bind
exactly the ports the stack publishes, and a port collision surfaces as a
container that exits immediately:

| port | held by | what to do |
|---|---|---|
| 3000 | `next dev` | stop it |
| 8000 | host `uvicorn` | stop it |
| 11434 | host Ollama | stop it — the container needs the port *and* the RAM |

Postgres (5432) and Redis (6379) do not collide: the host cluster runs on 55432
and Redis on 6399.

**Skip re-downloading the models.** `model-pull` fetches ~5.7GB, which this host
already has. Copy them into the volume instead:

```bash
docker volume create copilot_ollama_models
docker run --rm -v copilot_ollama_models:/dest \
  -v "$HOME/.ollama:/src:ro" alpine sh -c "cp -a /src/. /dest/"
```

Then the sequence from [§7](#7-reference-the-whole-sequence) **without**
`COMPOSE_FILE`, so no GPU overlay is applied:

```bash
cp .env.example .env
python -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(48))" >> .env
echo "PUBLIC_API_BASE=http://127.0.0.1:8000" >> .env
echo "ALLOW_LOCALHOST_API_BASE=1"            >> .env   # required: see section 6
echo "CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000" >> .env

docker compose build
docker compose up -d
docker compose run --rm migrate
docker compose run --rm model-pull      # skip if you copied the volume above
docker compose run --rm backend python scripts/index_filings.py NVDA AAPL
```

### Verifying it

Two commands, and they are the whole of the claim:

```bash
# The suite, inside the deployed image
docker compose run --rm backend python -m pytest -q

# The application, over HTTP, exactly as a browser would
python scripts/verify_deployment.py --base-url http://127.0.0.1:8000
```

`scripts/verify_deployment.py` checks health per dependency, signup, login,
`/auth/me`, a forged token, rate-limit headers, a real `/ask` with a tool call, a
real `/ask/stream` with token-by-token delivery, that the turn persisted, and
that a second user gets 404 on both. It talks HTTP only, so it is the same check
against the host, a container, or a cloud VM — which is what makes the comparison
meaningful rather than two different tests.

Then open `http://localhost:3000`, sign up, and ask a question.

### Results

**Status: not yet run — Docker is not installed on this host, and installing it
needs an administrator and a reboot.** This section gets the real numbers, or the
real failures, once the stack is up.

What is already done is the half that makes the comparison mean something: the
**baseline**, captured from the app running directly on the host, so
"containerized behaves identically" can be checked against a recorded result
rather than a memory of one.

```
$ python scripts/verify_deployment.py --base-url http://127.0.0.1:8000

1. health        4 dependencies checked separately, all 200
                 rate limiting: limiting=True policy=fail-open
                 models served: qwen2.5:7b, qwen2.5:1.5b, ...
2. auth          signup 201, login 200, /auth/me identifies the caller,
                 forged token 401, RateLimit-Remaining present
3. ask           200 in 243s   stop_reason=final_answer completed=True iterations=2
                 tools: ['calculate_ratio', 'final_answer']
                 "NVIDIA's gross margin in fiscal 2026 was 71.07%, calculated as
                  gross profit of $153.463 billion divided by revenue of $215.938
                  billion."
4. persistence   2 messages stored: ['user', 'assistant']
5. ask/stream    iteration, escalate, discarded, iteration, tool_start,
                 tool_result, iteration, escalate, done
                 134 token events, first at 9.0s
6. isolation     second user gets 404 on read and on stream

OK  all 23 checks passed
```

Worth noting what the stream trace shows, because it is the system working rather
than misbehaving: `discarded` is the grounding guard rejecting a first draft whose
figures traced to no tool result, and `escalate` is the router handing off from
the 1.5b model to the 7b. Both should appear in the containerized run too.

Also checked statically, which needs no Docker:

- Every `COPY` source in both Dockerfiles resolves on disk (15 and 3).
- Every Python package in the repo is in the backend image's `COPY` list, so no
  import can fail at run time for being absent from the build.
- `.next/standalone`, `.next/static` and `public` — the three paths the frontend
  runtime stage copies — are all produced by `npm run build`.

### A caveat about RAM on this particular host

15.7GB total, ~5GB free. Docker Desktop's WSL2 VM wants 2–4GB, and the Ollama
container holds `qwen2.5:7b` (~4.7GB) plus the 1.5b router (~1GB) resident
because `OLLAMA_MAX_LOADED_MODELS=2`. Running natively, free RAM on this host
already dipped to **0.08GB** with both models loading (`docs/INFERENCE.md`).
Adding a VM underneath makes it genuinely marginal.

If the container is killed or the machine swaps, set `AGENT_ROUTER_MODEL=` in
`.env` to run a single model. That disables the router split, which costs latency
but removes ~1GB and one resident runner. It is a constraint of this laptop, not
of the deployment: the cloud host the compose file is written for has neither the
RAM ceiling nor the competing desktop.

---

## Known gaps in this deployment

Honest list, because finding these at 2am is worse than reading them now.

- **No TLS.** Both public ports are plain HTTP, so bearer tokens cross the
  internet in clear text. Fine for a private demo on a firewalled IP; not fine
  for anything real. Fixing it properly means a reverse proxy (Caddy gets you
  automatic certificates in about ten lines) in front of both services, which
  also collapses the two origins into one and makes the CORS problem disappear.
- **One uvicorn worker**, deliberately: the agent loop is bounded by the single
  GPU behind Ollama, and a second worker would duplicate the embedding model in
  memory to contend for it.
- **No backups.** `pgdata` is a local Docker volume. `docker compose down -v`, or
  terminating the instance, is unrecoverable.
- **Postgres password defaults to `postgres`.** Not published beyond the compose
  network, but set `POSTGRES_PASSWORD` anyway.
- **`data/edgar` and `pgdata` grow without bound.** Nothing prunes either.
- **The eval harness is not wired into the deployment.** `python -m eval.compare`
  works in the container and is how you find out whether a config change helped;
  a full run is 60–90 minutes and nothing schedules it.
