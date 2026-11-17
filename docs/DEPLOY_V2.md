# Deploying version 2

What changes on top of the version 1 deployment in [DEPLOY.md](DEPLOY.md): the
same hosts (Render, Vercel, Neon, Upstash), a newer branch, two new tables and
their data. Names only; every value comes from your own `.env`, `.env.neon` or
the host's dashboard.

Prepared on 2026-11-17. Nothing here has been deployed.

## 0. Before deploying: open decisions

| item | state | why it matters |
|---|---|---|
| Precompute the signal pages | **built** (`5f55ed9`, `149b5eb`); needs the role and secret, section 6 | Without it the dashboard took 25-36 s and a stock page 30-40 s on the free tier; with it 0.04-0.34 s (measured, section 6). |
| "Ask the agent to explain" | hidden unless the build sets `NEXT_PUBLIC_ENABLE_AGENT_EXPLAIN=1` (`fe00bd2`) | The endpoint runs on local Ollama only; it is wired to the hosted model after the 2026-11-17 eval, behind the token caps. Leave the variable unset on Vercel until then. |
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
| `0008_precomputed_views` | `precomputed_views`: the daily stock pages | none: written by the daily job (section 6) and by the first visitor when a page is missing or stale |

`scripts/start.sh` runs `alembic upgrade head` on every Render start, so
deploying the merged code applies both migrations by itself. The data is loaded
from the laptop, after the migrations exist.

**Load steps** (PowerShell, from a checkout of the merged code, local stack not
needed except the bge-small weights for the cards):

```powershell
# 1. Point this shell at Neon. The value is read from .env.neon, never typed or printed.
$env:DATABASE_URL = ((Get-Content .env.neon) -match '^DATABASE_URL=')[0].Substring(13)

# 2. Schema (skip if Render has already started on the merged code)
alembic upgrade head                    # 0006, 0007, 0008
python scripts/check_migration.py       # models and migrations agree

# 3. Strategy cards: validate, then embed and write 182 sections
python scripts/index_strategies.py --check
python scripts/index_strategies.py

# 4. Prices (optional, see above): the five NSE stocks and the Nifty, 8 years
python scripts/load_prices.py --years 8

# 5. Back to the local database
Remove-Item Env:DATABASE_URL
```

**Check afterwards** (read only): `alembic_version` = `0008_precomputed_views`;
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

After deploy, `/health/ready` should report `"migration": "0008_precomputed_views"`.
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

## 6. The daily precompute (built)

**What it does** (`market/precompute.py`, `scripts/precompute_views.py`):

- After the NSE close, the job computes the dashboard and the five watchlist
  stock pages and option sets and stores them in `precomputed_views`.
- A stored page is served when it was computed after the latest weekday
  session close and by the same signal code (a hash of the `market/` sources).
  Otherwise the API computes the page for that visitor and stores it.
- The job reads prices without writing the price cache, so its role needs no
  write access there.

**Measured at Render's limits** (`--memory=512m --cpus=0.1`, the trial-merged
image, local pgvector database, 2026-11-17 05:47 UTC):

| call | before (section 5) | precomputed |
|---|---|---|
| dashboard | 24.8-36.4 s | 0.29 s, then 0.04 s |
| stock page (SBIN) | 29.6-33.3 s | 0.07 s, then 0.05 s |
| options (SBIN) | 30.2-31.4 s | 0.34 s, then 0.04 s |
| stock page with no stored page (LT.NS) | | 24.3 s (computed and stored), then 0.04 s |

Peak memory 156 MiB. The job itself, in a fresh venv with only
`requirements-precompute.txt` and connected as the restricted role: 11 pages,
0 failed, 45.4 s.

**The job's database role** (`deploy/neon-precompute-role.sql`):
`signals_precompute` can SELECT `price_bars`, `price_fetches` and
`strategy_cards`, and SELECT/INSERT/UPDATE `precomputed_views`. It has nothing
else, at most two connections, and a 120 s statement timeout. It is created
with SQL, because roles created in the Neon Console become members of
`neon_superuser`.

**Your steps, after the merge** (the workflow runs from `master` only):

1. Neon at `0008_precomputed_views`: after Render has started on the merged
   code, or with step 2 of section 2.
2. `gh auth status`: the GitHub CLI logged in to this repository's account.
3. From the repository root: `python scripts/create_precompute_role.py`.
   - It reads the owner URL from `.env.neon` and generates a password in
     memory.
   - It creates the role (or rotates its password) and applies the grants.
   - It checks, as the new role, that the grants work and the refusals hold.
   - Only then it passes the role's URL to
     `gh secret set NEON_PRECOMPUTE_DATABASE_URL`.
   - Neither the URL nor the password is printed. Running it again rotates
     both.
4. GitHub > Actions > precompute-signals > **Run workflow**. The log should
   end with `11 stored, 0 failed`.
5. Open the dashboard: the response carries `"served": {"from": "precomputed"}`.

**Without the script:** in the Neon SQL editor, as the owner, run
`CREATE ROLE signals_precompute WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '<a long random password>'`
and then `deploy/neon-precompute-role.sql`. Then add the role's connection
string under GitHub > Settings > Secrets and variables > Actions as
`NEON_PRECOMPUTE_DATABASE_URL`.

**Not built:** the options call still reruns the evaluation on the live path
(only the stored copy is fast). Reusing the stored report's verdict there
would cut the fallback for options.

## 7. After deploying

1. `/health/ready`: status ok, migration `0007_strategy_cards`.
2. Landing page: the four measured cards, Try the demo, no dev indicator.
3. Dashboard: Nifty strip and five cards with signals and coverage.
4. A stock page: the Signal tab shows the no-edge verdict and card excerpts
   (empty if step 2.3 was skipped); the Options tab shows a payoff.
5. Research: a US 10-K question and an NSE price question both answer.
6. CORS: no blocked requests in the browser console.
