# Deploying version 2

What changes on top of the version 1 deployment in [DEPLOY.md](DEPLOY.md): the
same hosts (Render, Vercel, Neon, Upstash), a newer branch, two new tables and
their data. Names only; every value comes from your own `.env`, `.env.neon` or
the host's dashboard.

Prepared on 2026-11-17. Nothing here has been deployed.

## 0. Before deploying: open decisions

| item | state | why it matters |
|---|---|---|
| Precompute the signal pages | **proposed, not built** (section 6) | On the free tier's 0.1 CPU the dashboard took 25-36 s and a stock page 30-40 s per visit (measured, section 5). |
| "Ask the agent to explain" | wired to local Ollama only | On Render it returns an error until it is wired to the hosted model (planned after the 2026-11-17 eval, behind the token caps). Hide the button until then, or leave it with its error state. |
| Calculator branch | merges after the 2026-11-17 eval and your approval | It is part of the merge order below; deploying without it is also possible. |
| Docker image | fixed (`5206c28`) | Before the fix the image had no `market/` package and the API exited at start-up. |

## 1. Merge order into master

A trial merge on a throwaway branch (`trial/merge-v2`, not master) found
conflicts only where listed, all resolved as stated. The merged tree has one
migration head (`0007_strategy_cards`), `scripts/check_migration.py` passes and
the frontend builds. The full suite on that tree (2026-11-17, 04:49-04:57
UTC, local pgvector database, standby blocked): **1,451 passed, 2 skipped, 0
failed**. The skips: the live injection test (needs the API proxy and so the
hosted model) and `test_parser`'s per-filing cases (the trial worktree had no
downloaded filings; `data/` is gitignored). A partial run, so not a merge
check by itself.

| # | branch | conflicts | resolution |
|---|---|---|---|
| 1 | `feat/deploy-prep` | none | |
| 2 | `feat/step4-corpus` | none | contains 1 |
| 3 | `fix/retrieval-fiscal-year` | none | |
| 4 | `feat/calculator-tool` | `docs/KNOWN_LIMITATIONS.md` | keep both sides: the two new step-4 entries, then the calculator's rewritten heading |
| 5 | `fix/live-demo-feedback` | none | |
| 6 | `feat/v2-market` | `agent/tools.py` (with 4: imports), `frontend/src/app/page.tsx`, `frontend/package-lock.json` (with 5) | imports: the union (`ERROR_BAD_INPUT, ERROR_NO_DATA, ERROR_UPSTREAM, error, ok` and `calculate`); frontend: keep 5's version (markdown chat) |
| 7 | `feat/frontend-redesign` | `frontend/package.json`, `package-lock.json`, `src/app/page.tsx`, `src/components/Markdown.tsx` | take 7's: it already holds 5's markdown chat (moved to `/research`) and the same react-markdown 10.1.0 / remark-gfm 4.0.1 |
| 8 | `docs/plan-v1-v2`, `docs/readme-hiring` | none | |

Without the calculator (order 1-3, 5-8) the only conflicts are the frontend
ones in rows 6 and 7. Before the real merge, run the full suite on the branch
as it will be merged (CLAUDE.md rule 2).

## 2. Neon: schema and data

**Now** (read-only check, 2026-11-17 04:30 UTC): Postgres 17.12, pgvector
0.8.7, migration `0005_pgvector_required`; 2,914 filing chunks, 62,387 facts
rows, 1 user; no `price_bars`, `price_fetches` or `strategy_cards`; 55 MB.

**New migrations**

| revision | adds | data needed |
|---|---|---|
| `0006_price_bars` | `price_bars`, `price_fetches`: the daily price cache | none required: filled on first request per ticker and refreshed after `MARKET_CACHE_TTL_HOURS` (24). Preloading avoids the first visitor paying the fetch. |
| `0007_strategy_cards` | `strategy_cards`: one row per card section, `vector(384)`, no index | **required** for the card text on the Signal tab and in explanations: 26 cards, 182 sections |

`scripts/start.sh` runs `alembic upgrade head` on every Render start, so
deploying the merged code applies both migrations by itself. The data is loaded
from the laptop, after the migrations exist.

**Load steps** (PowerShell, from a checkout of the merged code, local stack not
needed except the bge-small weights for the cards):

```powershell
# 1. Point this shell at Neon. The value is read from .env.neon, never typed or printed.
$env:DATABASE_URL = ((Get-Content .env.neon) -match '^DATABASE_URL=')[0].Substring(13)

# 2. Schema (skip if Render has already started on the merged code)
alembic upgrade head                    # 0006, 0007
python scripts/check_migration.py       # models and migrations agree

# 3. Strategy cards: validate, then embed and write 182 sections
python scripts/index_strategies.py --check
python scripts/index_strategies.py

# 4. Prices (optional, see above): the five NSE stocks and the Nifty, 8 years
python scripts/load_prices.py --years 8

# 5. Back to the local database
Remove-Item Env:DATABASE_URL
```

**Check afterwards** (read only): `alembic_version` = `0007_strategy_cards`;
`strategy_cards` = 182 rows; `price_bars` about 13,500 rows if step 4 ran
(5 x 2,246 + 2,241 locally). Size grows by a few MB.

## 3. Render (the API)

| field | change |
|---|---|
| Branch | `master` once merged. For a preview before merging, `feat/frontend-redesign` holds all of version 2 except the calculator. |
| Dockerfile path, Docker command, health check | unchanged: `./Dockerfile`, `sh scripts/start.sh`, `/health` |

Environment variables:

| name | change |
|---|---|
| `HOSTED_TOKENS_PER_DAY` | raise to 60000 as planned |
| `DEMO_QUESTIONS_PER_DAY` | raise to 10 as planned |
| `MARKET_CACHE_TTL_HOURS` | new, optional; default 24 |
| everything else | unchanged (`DATABASE_URL`, `REDIS_URL`, `JWT_SECRET`, `INTERNAL_TOKEN`, `GROQ_API_KEY`, `SEC_EDGAR_USER_AGENT`, `CORS_ORIGINS`, `ENVIRONMENT`, provider settings) |

After deploy, `/health/ready` should report `"migration": "0007_strategy_cards"`.
The container became healthy 79 s after start at `--cpus=0.1` locally; the
first request after Render's idle spin-down pays that, and the frontend shows
its "Waking the API" notice.

## 4. Vercel (the frontend)

| field | change |
|---|---|
| Production branch | `master` once merged (Settings > Git). To look first, deploy `feat/frontend-redesign` as a Preview. |
| Root Directory | unchanged: `frontend` |
| `NEXT_PUBLIC_API_BASE` | unchanged; read at build time |

A Preview deployment has its own origin. To use it against the API, add its URL
to `CORS_ORIGINS` (comma-separated, https, no trailing slash) and remove it
afterwards.

## 5. Free-tier performance (measured)

Image built from the trial-merged tree, run with
`docker run --memory=512m --memory-swap=512m --cpus=0.1` against the local
pgvector database, 2026-11-17 04:42-04:48 UTC, ASUS Vivobook X1504ZA
(i5-1235U). Each call twice; prices for the watchlist were already in the
cache (fetched the day before, within the 24 h TTL).

| call | first | second | memory after |
|---|---|---|---|
| `GET /market/watchlist` (dashboard) | 36.4 s | 24.8 s | 160 MiB |
| `GET /market/SBIN.NS/signal/report` (stock page) | 33.3 s | 29.6 s | 152 MiB |
| `GET /market/DIXON.NS/signal` | 7.7 s | 4.7 s | 162 MiB |
| `POST /market/SBIN.NS/backtest` (one strategy, 1 year) | 0.08 s | 0.06 s | 149 MiB |
| `GET /market/SBIN.NS/options` | 31.4 s | 30.2 s | 148 MiB |
| `GET /market/SBIN.NS/chart` (2 years) | 0.19 s | 0.78 s | 161 MiB |
| stock page for a ticker never cached (TATAPOWER.NS) | 36.6 s | 39.5 s | 155 MiB |

Memory peaked at 162 MiB of 512. Render's 0.1 CPU may not behave exactly like
Docker's `--cpus=0.1`; these are the local measurement, not Render's.

Where the time goes: each stock page and options call reruns the five-year
evaluation, including 200 random re-timings, in Python loops; the dashboard
runs the walk-forward engine for five stocks. All of it depends only on daily
closes.

## 6. Proposal: compute the signal pages once a day

**Measured cost at full speed:** on one uncapped core, the whole day's set
(dashboard, five stock reports, five option sets) took 34.8 s (4.1 + 16.0 +
14.7 s).

**Design**

- A table `precomputed_views (key, as_of, payload jsonb, computed_at,
  code_version)`, migration 0008. Keys: `watchlist`, `report:<ticker>`,
  `options:<ticker>`. About 60 KB per stock page, 5 KB for the dashboard.
- `scripts/precompute_views.py` computes the set for the watchlist and upserts
  it.
- A GitHub Actions workflow on a weekday cron an hour after the NSE close
  (15:30 IST, 10:00 UTC; so 11:00 UTC) runs it against Neon. It needs the Neon URL as a
  repository secret. It does not wake Render and uses no model.
- The endpoints serve the stored payload when its `as_of` is the latest
  completed session and its `code_version` matches. Otherwise they compute as
  now and store the result, so a missed job costs one slow visit, not all of
  them.
- Separately, the options call should reuse the stored report's verdict instead
  of rerunning the evaluation.

**Expected effect (inferred, not measured):** the dashboard and stock pages
become a database read of tens of kilobytes. A ticker outside the watchlist
still takes the live path, about 35-40 s.

**Not decided:** adding a GitHub secret and a scheduled workflow is your call.
The alternative without a secret is the lazy store alone: the first visitor of
the day per stock waits, everyone after that does not.

## 7. After deploying

1. `/health/ready`: status ok, migration `0007_strategy_cards`.
2. Landing page: the four measured cards, Try the demo, no dev indicator.
3. Dashboard: Nifty strip and five cards with signals and coverage.
4. A stock page: the Signal tab shows the no-edge verdict and card excerpts
   (empty if step 2.3 was skipped); the Options tab shows a payoff.
5. Research: a US 10-K question and an NSE price question both answer.
6. CORS: no blocked requests in the browser console.
