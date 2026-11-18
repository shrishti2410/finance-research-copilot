# Load test of the endpoints that do not call the model: CPU-bound at about 5 requests a second

Plan item 4: a light load test (10–25 concurrent users) on the endpoints that
never call a language model. It ran locally against the API image, in a
container capped like Render's free instance. **The live site was not
load-tested.**

## Result

The container's CPU sat at its 0.1-core cap at both levels. Memory peaked at
165 MiB of 512.

| users | requests | failed | req/s | p50 | p95 | p99 | max |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 836 | 3 (0.36%) | 2.82 | 240 ms | 1.8 s | 4.0 s | 10.4 s |
| 25 | 1,406 | 2 (0.14%) | 4.71 | 1.5 s | 6.8 s | 10.0 s | 11.4 s |

- **Saturation.** Going from 10 users to 25 multiplied throughput by 1.7
  (2.82 → 4.71 req/s) and the median latency by 6 (240 ms → 1.5 s). At the
  cap the container serves about 5 requests a second, and extra users wait in
  line.
- **Every request was served from stored data.** These endpoints only read
  and serialise: the stock pages from the daily precompute (checked before the
  run, each `served: precomputed`), the prices from the Postgres cache. So the
  limit is the 0.1 CPU spent on JSON, the database driver and the HTTP stack.
  **Inferred** from the CPU readings; no profile was taken.
- **The 5 failures were dropped connections**, "Remote end closed connection
  without response" or "connection aborted", with no HTTP error. The container
  logged no tracebacks or errors.
  - **Probable cause, inferred, not shown:** uvicorn closes a keep-alive
    connection after 5 idle seconds (its default; `scripts/start.sh` sets no
    `--timeout-keep-alive`). Locust waits 1–5 s between a user's requests on
    the same connection, so a request can arrive just as the server closes
    it.
  - A browser retries such a request; Locust counts it as a failure.
  - **Proposal, not done:** `--timeout-keep-alive 15`.

### Per endpoint

10 users:

| endpoint | requests | failed | p50 | p95 | p99 |
|---|---:|---:|---:|---:|---:|
| `/market/strip` (public) | 157 | 1 | 88 ms | 1.0 s | 2.2 s |
| `/market/watchlist` | 154 | 0 | 290 ms | 2.3 s | 3.5 s |
| `/market/[t]/signal/report` | 197 | 1 | 220 ms | 1.8 s | 3.5 s |
| `/market/[t]/options` | 111 | 1 | 350 ms | 2.3 s | 4.5 s |
| `/market/[t]/chart` (2 years, 5 indicators) | 106 | 0 | 510 ms | 3.9 s | 8.1 s |
| `/auth/me` | 54 | 0 | 200 ms | 1.3 s | 1.6 s |
| `/health` | 57 | 0 | 46 ms | 1.0 s | 1.4 s |

25 users:

| endpoint | requests | failed | p50 | p95 | p99 |
|---|---:|---:|---:|---:|---:|
| `/market/strip` (public) | 260 | 1 | 1.0 s | 3.9 s | 6.9 s |
| `/market/watchlist` | 259 | 0 | 1.8 s | 7.2 s | 9.8 s |
| `/market/[t]/signal/report` | 346 | 1 | 1.7 s | 7.4 s | 11 s |
| `/market/[t]/options` | 192 | 0 | 2.4 s | 7.3 s | 10 s |
| `/market/[t]/chart` | 163 | 0 | 2.4 s | 10 s | 11 s |
| `/auth/me` | 95 | 0 | 1.2 s | 3.5 s | 6.6 s |
| `/health` | 91 | 0 | 480 ms | 3.3 s | 6.1 s |

- **`/health` slows too:** a 480 ms median at 25 users, for a route that does
  nothing. That points at the shared CPU, not at the database.
- **The chart is the heaviest:** two years of bars, with indicators computed
  per request and not stored.

### What would change the picture (not measured)

- **More CPU.** A paid instance with a full core is 10 times the cap.
  Inferred, not run.
- **The landing page now comes from Vercel's cache** (`feat/cold-start`), so
  visitors to it do not reach this API at all, except for the browser's
  strip refresh.
- **The chart endpoint** could cache its computed series per day, as the
  stock pages do.

## How it was run

| | |
|---|---|
| image | `frc-api:master-df30f35`, built from master `df30f35` with the repository's Dockerfile |
| container | `docker run --memory=512m --memory-swap=512m --cpus=0.1` (Render's free instance: 512 MB, 0.1 CPU), env from the local `.env` with database and Redis pointed at the Docker host; **rate limiting off**. Every simulated user comes from one IP, so the limiter would have measured itself. Production runs with it on. |
| data | the local pgvector database (the same schema as Neon, at 0009) and Redis; the local precompute was re-run on master's code first (17 pages stored, 0 failed) |
| load | Locust 2.46.5, `benchmarks/locustfile_market.py`; 25 throwaway local accounts made beforehand (sign-up's bcrypt not measured). Mix by weight: stock signal report 4, strip 3, watchlist 3, options 2, chart 2, `/auth/me` 1, `/health` 1. Think time 1–5 s. |
| command | `locust -f benchmarks/locustfile_market.py --host http://127.0.0.1:8100 --headless -u <U> -r <U> -t 5m --csv <out>/u<U> --only-summary`, U = 10 then 25, 30 s apart |
| when | 2026-11-18: 10 users 05:25:19–05:30:23 UTC, 25 users 05:30:53–05:35:56 UTC |
| machine | Windows 11 laptop, Docker 29.9.3, plugged in; standby blocked for the run (`SetThreadExecutionState`). The load generator ran on the same laptop, outside the container's CPU cap. |
| container metrics | `docker stats` every 5 s: 100 samples; CPU at the cap (9.8–11.1% of one core) under load; memory 144.5 MiB before, 165.4 MiB peak, 151.9 MiB after |

Raw output, gitignored: `benchmarks/results/2026-11-18-load-market/`
(`u10_*.csv`, `u25_*.csv`, Locust logs, `docker_stats.txt`).
