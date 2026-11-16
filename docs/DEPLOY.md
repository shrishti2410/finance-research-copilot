# Deploying the public demo on free tiers

A public demo of the copilot on hosts that cost nothing: the API on Render, Postgres
on Neon, Redis on Upstash, the model on Groq, and the front end on Vercel. This file
is the list of steps to do by hand in each dashboard, in order.

For a single GPU machine running the whole stack, see [DEPLOYMENT.md](DEPLOYMENT.md)
instead.

> **Not deployed yet, and one blocker is open.** Nothing in this file has been run
> against a real account. The API measured 581 MB resident with the embedding model
> loaded (Windows, 2026-11-16), and Render's free instance has 512 MB. See
> [Before you start](#0-before-you-start-the-memory-blocker).

## Contents

0. [Before you start: the memory blocker](#0-before-you-start-the-memory-blocker)
1. [The hosts, and why these](#1-the-hosts-and-why-these)
2. [Neon: the database](#2-neon-the-database)
3. [Load the filings and facts into Neon, from your laptop](#3-load-the-filings-and-facts-into-neon-from-your-laptop)
4. [Upstash: Redis](#4-upstash-redis)
5. [Groq: the model key and the daily budget](#5-groq-the-model-key-and-the-daily-budget)
6. [Render: the API](#6-render-the-api)
7. [Vercel: the front end](#7-vercel-the-front-end)
8. [Close the loop: CORS](#8-close-the-loop-cors)
9. [Check it works](#9-check-it-works)
10. [What protects the Groq allowance](#10-what-protects-the-groq-allowance)
11. [What is not verified](#11-what-is-not-verified)

---

## 0. Before you start: the memory blocker

The API embeds each search query with bge-small. The model runs on torch, and torch
and the model weights sit in memory for the life of the process.

| | measured on this laptop, 2026-11-16 |
|---|---|
| API process after start-up | 151 MB |
| after the first query embedding (model loaded) | 581 MB |
| model load time | 39.8 s on this laptop's CPU |
| Render Free instance | 512 MB RAM, 0.1 CPU (Render docs, compute plans) |

Measured with `psutil` on Windows 11, Python 3.11, CPU torch. The Linux container
was **not measured** and may be smaller or larger. If it is over 512 MB, Render
restarts the service at the first search.

**Options, in the order I recommend:**

1. **Embed queries with ONNX instead of torch.** Same model, exported once to ONNX,
   run with `onnxruntime`. Smaller and faster to load, and torch leaves the API
   image (ingestion can keep it). It is only acceptable if its query vectors match
   torch's and the retrieval eval does not move, which has to be measured on its own
   branch before it ships.
2. **Measure the Linux container first.** Build the image and run it with
   `docker run --memory=512m`. It may already fit.
3. **A host with more memory.** Each costs money or needs a card.

Do not deploy the API until one of these is settled.

---

## 1. The hosts, and why these

From each provider's own documentation, read 2026-11-16. Nobody signed up for
anything.

| role | host | card | free-tier limits that matter here |
|---|---|---|---|
| front end | Vercel Hobby | not asked for in the docs | 100 GB transfer/month, 1M function calls/month. Personal or non-commercial use only (Terms, section 4). |
| API | Render Free web service | docs imply optional: they describe what happens "if you haven't added a payment method" | 512 MB, 0.1 CPU, 750 instance hours/month. Sleeps after 15 min idle and takes about 1 min to wake. No pre-deploy command, no persistent disk. |
| Postgres + pgvector | Neon Free | **no** ("no credit card required") | 1 GB per project, 100 compute-hours/month. Scales to zero after 5 min and wakes in a few hundred ms. pgvector 0.8.0. Never paused for inactivity. |
| Redis | Upstash Free | not asked for; entering one upgrades you to pay-as-you-go | 500K commands/month, 250 MB, 1 database. TLS always on. |
| model | Groq free tier | already in use | `openai/gpt-oss-120b`: 200,000 tokens/day, 8,000/minute, 1,000 requests/day (Groq's 429 headers, 2026-11-15) |

Rejected:

- **Koyeb.** Requires a card and places a $29 hold.
- **Supabase.** Pauses free projects after a week idle, and its direct connections
  are IPv6-only.
- **Render Postgres.** Deleted 30 days after creation.
- **Render Key Value.** Loses its data on every restart.

Our whole local database is 19 MB (measured), well inside Neon's 1 GB.

---

## 2. Neon: the database

1. Sign up at neon.com. Choose the **Free** plan.
2. **Create project.**
   - Postgres version **17**.
   - Region: the one closest to the Render region you will pick in step 6, e.g.
     AWS Europe (Frankfurt) with Render Frankfurt, or AWS US East with Render Ohio
     or Virginia.
3. Open **Connect**, and copy the connection string with **"Connection pooling"
   turned off** (a host without `-pooler` in it). It ends in `?sslmode=require`,
   sometimes with `&channel_binding=require` too. Both are fine: the app turns them
   into asyncpg's TLS setting (`core/config.py`, `_asyncpg_url`).
   - Why not the pooled one: the app uses asyncpg, which caches prepared statements
     per connection. That cache does not survive a transaction-mode pooler. This is
     general asyncpg behaviour and was not tested against Neon.
4. Keep the string somewhere private. It is `DATABASE_URL` in steps 3 and 6. It
   contains the password: never commit it or paste it into an issue.

pgvector needs no dashboard step. Migration `0002_filing_chunks` runs
`CREATE EXTENSION` itself.

---

## 3. Load the filings and facts into Neon, from your laptop

The free web service cannot run one-off jobs, and embedding filings on 0.1 CPU would
take hours. So the schema and the data go in from this machine.

In a terminal at the repo root, with the Neon string in an environment variable for
this one shell only (PowerShell shown; it is not written to `.env`):

```powershell
$env:DATABASE_URL = "<the Neon connection string from step 2>"

alembic upgrade head                 # schema, including pgvector
python scripts/index_filings.py      # the pinned NVDA and AAPL 10-Ks; minutes on CPU
python scripts/index_facts.py        # SEC XBRL facts for NVDA and AAPL

Remove-Item Env:DATABASE_URL         # back to the local database
```

`SEC_EDGAR_USER_AGENT` comes from your `.env` as usual.

Check before moving on. In the Neon **SQL Editor**:

```sql
SELECT count(*) FROM filing_chunks;  -- 113 locally on 2026-11-16
SELECT count(*) FROM xbrl_facts;     -- 8151 locally on 2026-11-16
SELECT version_num FROM alembic_version;  -- 0004_public_demo
```

---

## 4. Upstash: Redis

1. Sign up at console.upstash.com. Do **not** enter a card: that moves the account
   to pay-as-you-go.
2. **Create database.**
   - Type: Redis.
   - Region: the same area as Render and Neon.
   - Eviction off.
3. On the database page, copy the endpoint, port and password, and assemble:
   `rediss://default:<password>@<endpoint>:<port>` (two s's: TLS). That is
   `REDIS_URL`. The Upstash docs show the same form for ioredis, and say the
   password is the token: "There is no separate password."

Redis holds only the per-minute rate-limit counters. If it is unreachable, the
limiter fails open (`RATE_LIMIT_FAIL_OPEN=true`), and `/health/redis` says so. The
daily caps are in Postgres, not Redis, so an Upstash outage does not lift them.

---

## 5. Groq: the model key and the daily budget

The deployed app and your evals share Groq's free daily allowance of 200,000 tokens
for `openai/gpt-oss-120b`. A hosted eval run used about 376,000 (run 1 on
2026-11-15, the full 40 cases), so the two cannot both run on the same day as it
stands.

1. In console.groq.com > **API Keys**, create a key named for the deploy, so it can
   be revoked on its own. It is `GROQ_API_KEY` in step 6.
   - Whether a second key in the same organisation has its own allowance or shares
     one is **not verified**. Assume it shares.
2. Choose `HOSTED_TOKENS_PER_DAY`, the cap on what visitors may spend per UTC day.
   - A suggested start: **60,000**. That leaves room for single-question dev work
     on the same key.
   - On 2026-11-15 a hosted question averaged about 9,400 tokens (run 1:
     376,352 tokens over 40 questions). That is history from a different commit,
     not a measurement of the deployed code, so 60,000 is roughly six questions a
     day.

---

## 6. Render: the API

1. Sign up at render.com with GitHub. Skip any payment step.
2. **New > Web Service**, and connect this repository.
3. Settings:

   | field | value |
   |---|---|
   | Name | e.g. `copilot-api`. The URL becomes `https://copilot-api.onrender.com` (or with a suffix if the name is taken) |
   | Region | the one next to Neon and Upstash |
   | Branch | `master` |
   | Runtime / Language | **Docker** |
   | Dockerfile path | `./Dockerfile` |
   | Docker command | `scripts/start.sh` |
   | Instance type | **Free** |
   | Health check path | `/health` |
   | Auto-deploy | your choice. Off means a push to master does not redeploy |

4. **Environment**: add every variable in
   [`deploy/free-tier.env.example`](../deploy/free-tier.env.example) with real
   values.
   - Each placeholder in that file is refused at boot under
     `ENVIRONMENT=production`, so a missed one stops the service with a message
     naming it. The message never prints the value.
   - Leave `CORS_ORIGINS` as the placeholder for now. Step 8 sets it.
   - Set `DEMO_QUESTIONS_PER_DAY` to something like **10**, consistent with the
     token budget (about 9,400 tokens a question).
5. **Create Web Service.**
   - The first build installs torch and bakes the model weights into the image.
     How long that takes on Render was not measured.
   - On start, `scripts/start.sh` runs `alembic upgrade head` (a no-op after
     step 3), then starts uvicorn on Render's `$PORT`.
6. Open `https://<your-service>.onrender.com/health/ready`. You want
   `"status": "ok"` and `"migration": "0004_public_demo"`.

Expected start-up failures and what they mean:

| log line | cause |
|---|---|
| `ENVIRONMENT=production, but: - ...` | a placeholder or unsafe value; the line names it |
| `alembic ... could not connect` | `DATABASE_URL` wrong, or the Neon project is suspended (compute-hours used up) |
| the service restarts at the first question | the memory blocker in section 0 |

---

## 7. Vercel: the front end

1. Sign up at vercel.com with GitHub (Hobby).
2. **Add New > Project**, and import this repository.
3. Settings:

   | field | value |
   |---|---|
   | Root Directory | `frontend` |
   | Framework Preset | Next.js (detected) |
   | Environment Variable | `NEXT_PUBLIC_API_BASE` = `https://<your-service>.onrender.com` (no trailing slash) |

   `NEXT_PUBLIC_API_BASE` is read at **build** time. Changing it later needs a
   redeploy.
4. **Deploy.** Note the production URL, e.g. `https://copilot-demo.vercel.app`.

---

## 8. Close the loop: CORS

1. In Render > the service > **Environment**, set
   `CORS_ORIGINS=https://copilot-demo.vercel.app`: your exact Vercel production
   URL, https, no trailing slash.
   - Production refuses `*`, `http://` origins and trailing slashes.
   - Vercel preview URLs are different origins and are not allowed unless listed
     here, comma-separated.
2. Save. Render redeploys.

---

## 9. Check it works

1. Open the Vercel URL. The sign-in page shows **Try the demo**, and no "Create
   one" link (`SIGNUP_ENABLED=false`).
   - If the page says the API is unreachable, the Render service is asleep. Wait
     about a minute and reload.
2. Click **Try the demo** and ask: "What was NVIDIA's revenue in fiscal 2026?"
3. Check the caps:
   - Ask more than `DEMO_QUESTIONS_PER_ACCOUNT` questions from one demo account.
     The next one is refused with a 429 that says the limit resets at 00:00 UTC.
   - In the Neon SQL Editor:
     `SELECT * FROM usage_counters WHERE day = current_date;`
     The `hosted_tokens` row should grow by a few thousand per question.
4. `https://<service>.onrender.com/health/redis` should say `"limiting": true`.

`scripts/verify_deployment.py` signs up a user, which `SIGNUP_ENABLED=false`
refuses. Use the checks above, or run it once with signup temporarily on.

---

## 10. What protects the Groq allowance

The Groq key never leaves the server. The front end talks to `/ask`. The model
proxy at `/v1` serves a hosted provider only to the app's own per-process token
(`api/inference_proxy.py`), so a visitor cannot call Groq through it.

| guard | setting | where |
|---|---|---|
| all visitors together, tokens per UTC day | `HOSTED_TOKENS_PER_DAY` | `api/quotas.py`, checked before each question, counted after it |
| all demo accounts together, questions per day | `DEMO_QUESTIONS_PER_DAY` | `api/quotas.py`, reserved atomically before the run |
| one demo account, questions per day | `DEMO_QUESTIONS_PER_ACCOUNT` | the same |
| new accounts | `SIGNUP_ENABLED=false`; `/auth/demo` hands out a fresh isolated account | `api/routes_auth.py` |
| request rate | `RATE_LIMIT_*` (per minute; `/auth/*` 10 per minute) | `api/rate_limit.py` |

The token cap stops new questions once the day's total has reached it. Questions
already running finish, so the overshoot is a few thousand tokens per question in
flight. All caps reset at 00:00 UTC. Groq's own daily window is rolling, not
midnight-to-midnight, so set the cap with a margin.

---

## 11. What is not verified

Stated so nobody mistakes this file for a tested runbook:

- **Not run.**
  - No account was created on any of these hosts.
  - The Docker image was not built for this change.
  - Nothing was deployed.
- **Memory on Linux.** Not measured (section 0).
- **Render's `X-Forwarded-For`.** Not checked. Until it is,
  `RATE_LIMIT_TRUST_PROXY=false`. The effect is that every visitor looks like
  Render's proxy to the per-IP limits: the anonymous limit (20 a minute) and the
  `/auth` limit (10 a minute) are shared by all visitors. That is safe in the
  direction that matters, since it limits more, not less. It also means 10 demo
  accounts a minute across everyone.
- **Neon's direct-connection limit** on Free. Not stated in its docs. The pool is
  set small (3 + 2) for that reason.
- **Upstash commands per question.** Not measured. 500K a month is the budget.
- **Whether a Groq key per deploy has its own allowance.** Not verified.
- **Vercel and the Render cold start.** The browser calls Render directly, not
  through a Vercel rewrite, because Vercel caps a proxied request at 120 s (its
  limits page) and an answer can take longer while Render wakes up. Whether
  streaming works through a rewrite is not stated in Vercel's docs.
- **Authentication.** Tokens stay in the `Authorization` header, not a cookie.
  - The front end and the API are on different sites (`vercel.app`, `onrender.com`),
    so an auth cookie would be a third-party cookie, which several browsers block.
  - A header token is also not sent automatically by the browser, so there is
    nothing for a cross-site request to ride on, and CORS runs without credentials.
  - The cost is that the token sits in `localStorage`, readable by any script on
    the page. It lasts 30 minutes (`JWT_EXPIRE_MINUTES`).
