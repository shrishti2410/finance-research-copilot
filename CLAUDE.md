# CLAUDE.md

Finance Research Copilot is a FastAPI and agent-loop backend with a Next.js client.
It answers questions about public companies from SEC 10-K filings (RAG) and live
tools. The model is reached through an OpenAI-compatible proxy: Qwen on Ollama
locally, or a hosted provider set in `.env`. Start with `docs/PROJECT.md`.
`frontend/CLAUDE.md` adds rules for the Next.js code.

## Plan (set 2026-11-17, after the version 2 deploy)

Versions 1 and 2 are live (master `b9499ba`, Render and Vercel). Work through
the items below in order.
- **Each item gets its own branch, with tests written first.**
- **Each ends with a short report.**
- **Diffs are shown before merging to master.**
- **Nothing is deployed without the user's yes.** A push to master redeploys
  Render and Vercel, so it counts as a deploy.

**Tonight (small, no Groq tokens):**
- **A. CI fix.**
  - The two environment-dependent test failures on master:
    `test_deploy` (`SEC_EDGAR_USER_AGENT` from CI's environment) and
    `test_hybrid_retrieval` (the AAPL dividend ranking on CI's two-company
    corpus).
  - The eval-gate question: what it runs, whether it spends Groq tokens, and
    whether it should run on every push.
- **B. Live-site fixes.**
  - The chart tab fits the data, with about 6 to 12 months visible, and shows
    RSI, MACD and Bollinger.
  - The landing copy says 8 stocks (5 NSE + 3 US), not five NSE.
- **C. End-to-end live check with screenshots.**
  - Pages: landing, dashboard, one NSE and one US stock with all tabs, at phone
    width.
  - Confirm the pages are served from precomputed data.

**From 2026-11-18 (the Groq allowance resets at 00:00 UTC):**

0. **Guard fixes, first.** Each one is a grounding or advice guard change:
   tests first, then show the diff and wait. In this order:
   - **Citation markers read as figures.**
     - The model writes markers such as `【0†value】`.
     - The guard reads the `0` as an untraced figure and puts the
       `[Unverified]` banner on a correct answer.
     - This happened to 4 passing cases in the 2026-11-17 calculator run
       (`benchmarks/HOSTED_RUN_2026-11-17_CALCULATOR.md`).
   - **A per-request token budget.**
     - No request to the model may exceed about 7,000 tokens, under Groq's
       8,000 tokens-a-minute limit. Two 413s in the calculator run were
       estimated at 8,100 and 8,250 tokens.
     - Trim large tool results rather than removing tools: keep the relevant
       table rows and sections, and say in the result what was trimmed.
     - Log the request size for every model call.
   - **Unit-aware grounding.** A percentage must match a percentage, or a ratio
     times 100 (the SBIN "5%" case).
   - **Price-prediction language.** The advice guard flags it.

   Then:
   - **The grader extension:** an "expected: decline" outcome and a count unit.
     Tests first, prove the 40 dev cases rescore identically, and show the diff.
   - **Freeze the held-out set:** commit it, and put the hash in the record,
     before any tuning.

**Site quality phase (set 2026-11-18).** It starts once the redesigned
landing page is live, and comes before items 1–6. Item 0 may run alongside
it.
- Each change on its own branch, with screenshots and the diff shown before
  merging.
- **Nothing that costs money:** free tiers only. Check a service's terms and
  limits before relying on it, and record what was checked.

*Tier 1, now:*
1. **Cold start.**
   - A GitHub Actions cron pings the API's `/health` every 14 minutes.
     Before adding it, confirm in Render's published terms and free-tier
     limits (750 instance-hours a month) that this is allowed and fits.
     Record the source and the date.
   - The landing page and the dashboard render from Vercel's cache/ISR even
     when the API is asleep: stale data, clearly dated, never an error.
   - **Decided 2026-11-18:** the user confirmed the API is the workspace's only
     Render service.
     - Keep-warm runs 02:30–18:30 UTC (08:00 to midnight IST), pinging
       `/health` only, never `/health/ready`, so Neon can suspend.
     - The hours: 80 pings a day, at most 14 minutes apart, keep the service
       up 02:30–~18:39, 16.15 h a day. That is 501 h in a 31-day month (484 h
       in a 30-day one), out of Render's 750 free hours per workspace. About
       249 h remain for visits outside the window; each visit keeps the
       service up at least 15 min.
     - `tests/test_keep_warm.py` pins the window, the 14-minute gap, the
       hours margin, and `/health` only.
2. **Sharing and discovery.** Open Graph and Twitter meta tags; a 1200x630
   preview image; a favicon; a title and description for every page; a
   sitemap and `robots.txt`.
3. **Research chat streams.**
   - Answers come over `/ask/stream`.
   - Each tool step shows live, in plain words.
   - Source cards preview the cited passage, with its accession and section.
4. **About the author.**
   - A "Built by Shrishti Chandak" section, and an `/about` page with
     LinkedIn, GitHub and a resume link.
   - The user supplies the URLs: use placeholders until then, and never
     invent them.
5. **Demo recording.** A script that records a 60–90 s video or GIF of the
   main flow, for the landing page and the README. Like the screenshot
   script, it uses a production build and closes its browser on every exit
   path.
6. **Quality bars, measured before and after:**
   - Lighthouse ≥ 95 in all four categories for `/` and `/dashboard`;
   - zero axe violations;
   - full keyboard navigation;
   - a text or table alternative for every chart.

   Both the "before" and the "after" figures come from commands run on the
   code they describe (rule 4).

*Tier 2, after item 3 (evaluation rigor):*
- term tooltips linked to a glossary;
- an "as of" timestamp on every figure;
- the tab and the range in the URL;
- a friendly daily-limit message with a reset countdown;
- follow-up suggestions, and copy and share on answers.

*Tier 3, after Tier 2:*
- Playwright end-to-end tests in CI, desktop and phone, with the model
  mocked;
- free uptime monitoring;
- Sentry's free tier;
- Vercel Analytics.

  Each within its free plan, checked first.

1. **Claim-level verification with independent recomputation.**
   - Recompute every derived figure (growth, margin, ratio) from its cited
     operands, and reject on a mismatch.
   - Record per claim whether it is supported, unsupported or conflicting.
   - This is a grounding-guard change, so show the diff and wait.
2. **Quant honesty.**
   - (a) An exposure-matched benchmark: buy-and-hold scaled to the signal's
     average exposure, with the rest in cash. Report CAGR, max drawdown,
     Sharpe, Sortino, exposure, turnover and cost drag for the signal,
     buy-and-hold and the exposure-matched benchmark.
   - (b) An ensemble ablation: the best single rule, equal-weight, majority,
     performance-weighted and regime-weighted. Add a correlation matrix of the
     16 voting rules.
   - (c) A three-universe evaluation: development, validation and final test,
     plus a final untouched time period. Freeze all parameters and commit them
     before the final test.
   - If there is no risk-adjusted edge, say so, and reposition the signal as a
     strategy-audit tool.
3. **Evaluation rigor.**
   - `docs/EVALUATION.md`: what the tests cover and the dev against held-out
     sets.
   - Retrieval metrics (Recall@5, MRR, nDCG), reported separately from answer
     correctness.
   - A held-out run with Wilson intervals and abstention.
4. **System design.**
   - `docs/SYSTEM_DESIGN.md`: failure boundaries, data flows, concurrency,
     retries, caching and TTLs, staleness.
   - A light load test (10 to 25 concurrent users) on the non-LLM endpoints
     only: p50/p95/p99, error rate, memory.
   - A backup LLM provider, with failover. On a provider's 5xx or a timeout,
     the turn moves to the second provider rather than ending.
   - Release the database connection during model calls. A chat run must not
     hold a pooled connection while it waits on the model; today
     `DB_POOL_SIZE + DB_MAX_OVERFLOW` (15) caps the questions in flight
     (`db/base.py`).
   - An alert when the daily precompute job fails, which today shows only as a
     red run in GitHub Actions.
   - External uptime monitoring of `/health/ready`, on a free plan.
   (Open questions from the 2026-11-17 draft of `docs/SYSTEM_DESIGN.md`.)
5. **Product.**
   - A Strategy Lab comparison, extending `/backtest`: strategies side by side
     on the same stock, period and costs.
   - A data provenance and observability page.
6. **Futures.** Turn the Options tab into a "Derivatives" tab that adds:
   - cost-of-carry fair value;
   - the NSE lot size;
   - margin;
   - leveraged P&L and daily mark-to-market scenarios.

   Label everything as an estimate: there is no live NSE futures data. Reuse
   the options pricing and payoff code.

Facts to keep straight: the signal has **16 voting rules**. The **26 strategy
cards** are handbook cards, 8 of them for options.

**Out of scope for now:** Celery, a full Grafana stack, a portfolio engine,
paper trading, live option chains, and more strategies or indicators beyond
this list.

These five rules are not optional.

## When to stop for approval

Work without stopping to ask, except before changing any of these three. For
them, show the diff and wait:

- the grounding guard (`agent/grounding.py`, the `[Unverified]` path in
  `agent/orchestrator.py`, `core/numerics.py`)
- the grader (`eval/metrics.py`, `eval/judge.py`, the noise band, the CI floors)
- the eval questions (`eval/questions.json`, `eval/build_questions.py`)

Everything else proceeds on your own judgement: report what you did and what
you found, including anything that went wrong.

## 1. A feature branch per task, small commits

- Never commit to `master`. Before the first edit of a task, create a branch from
  an up-to-date `master`, named for the task: `fix/...`, `feat/...`, `docs/...`,
  `eval/...` or `bench/...`.
- Commit in small, self-contained steps, each one passing its affected tests
  (rule 2) and each doing one thing. Say *why* in the message, as the existing
  history does.
- Never rewrite published history. That means no force-push, no `reset --hard`
  over pushed commits, and no amending a pushed commit. Push and open PRs only
  when asked.

## 2. Affected tests before every commit, the full suite before every merge

- **Before each commit,** run the tests the change affects: the test files for
  the modules you touched, plus any test that imports them. Name what you ran
  in the commit message.
- **Before merging a branch into `master`,** run the full suite once on the
  branch as it will be merged:

  ```bash
  python -m pytest -p no:cacheprovider -q -rs
  ```

- **Tests default to local Ollama** and need no API key, so CI runs without one.
  Do not make a test depend on a hosted provider.
- **A skipped test was not run.** Every external dependency (Postgres, Redis,
  Ollama, bge-small, the Qwen tokenizer, the indexed filings) turns into a
  skip when it is missing, so a green run with skips is a partial run.
- **Before the full suite, bring the stack up.** Nothing restarts after a
  reboot. Postgres runs on 55432 (start command in `docs/DATABASE.md`), Redis
  per `REDIS_URL` in `.env`, and Ollama with both `qwen2.5:7b` and
  `qwen2.5:1.5b` pulled. The filings must be indexed with
  `python scripts/index_filings.py` and the structured facts with
  `python scripts/index_facts.py`. Block laptop standby for the run.
- **If a dependency cannot be brought up,** record the skip count and its
  reasons in the commit or merge message, and say so in your report. Never
  describe such a run as passing.
- **Never commit or merge with a failing test.** This includes tests you
  believe are unrelated. Report them instead.
- **CI is stricter:** it fails on any skip and on fewer than 900 tests
  (`scripts/ci_check_junit.py`).
- **Extra checks for some changes:**
  - `db/models.py` or `migrations/`: also run `python scripts/check_migration.py`.
  - `frontend/`: also run `npm run lint` and `npm run build` in `frontend/`.

## 3. Never raise a score by moving the goalposts

You may never change any of the following to make a score, a test or a
comparison come out better. If a task looks like it needs one of them changed,
stop and ask.

- **The question set:** expected answers, tolerances or units in
  `eval/questions.json`; the tolerance rules in `eval/build_questions.py`
  (`mag_tol`, price tolerances); dropping or skipping cases.
- **The grader:** `eval/metrics.py` (number extraction, unit conversion,
  pass/fail), `eval/judge.py`, the noise band (`MIN_CASES`, `--min-cases`,
  `EVAL_MIN_CASES`) and the CI floors in `scripts/ci_check_junit.py` and the
  workflows.
- **The baseline:** do not promote `eval/baselines/current.json` without being
  asked.
- **The grounding guard:** `agent/grounding.py` (`MATCH_RELATIVE`,
  `RESCALINGS`, `DERIVED_RESCALINGS`, `DERIVATION_LIMIT`, what counts as
  evidence); `MAX_GROUNDING_RETRIES` and the `[Unverified]` path in
  `agent/orchestrator.py`; the figure and form rules in `core/numerics.py`.
  This guard is what stops the agent stating numbers no tool returned.
- **The other guards:** `agent/advice.py` and `agent/untrusted.py`, and the
  system prompt rules they pair with.
- **Safety tests:** any test marked `@pytest.mark.safety`. Weakening, skipping
  or `xfail`-ing one counts as weakening the guard it protects.

**Legitimate changes still need the user's go-ahead, in their own commit:**
- Regenerating `questions.json` with `python -m eval.build_questions` after the
  corpus changes.
- Fixing a scorer bug that misreads a correct answer.

Never combine either one with an agent change in the same commit.

## 4. Every accuracy or latency claim comes from a command you ran

- **Which claims:** any number about accuracy, recall, latency, throughput,
  tokens/s, cost or memory, in a doc, commit message, PR or reply. It must come
  from a command you ran in this task, on the code you are describing.
- **Not acceptable sources:** numbers from memory, from docstrings, from older
  docs, or from a run on different code. Quote those only as history, and name
  their source. Prices come from the provider's published pricing on the day,
  with the source and date stated.
- **Which model for evals:** during development, run evals on the hosted model
  set in `.env`, because a full run takes minutes instead of 85. Run Qwen on
  Ollama once at the end of a piece of work for the comparison. A hosted run
  and a Qwen run are never compared as if they were the same system: name the
  model on both sides.
- **Where outputs go:**
  - Save the raw output under `benchmarks/results/<YYYY-MM-DD>-<topic>/`. That
    directory is gitignored because it can hold bearer tokens
    (`users.json`). This includes `eval_run.json`, `eval.compare` text,
    retrieval-eval output and Locust CSVs.
  - Write the committed record as `benchmarks/<topic>.md`.
- **What the committed record states:**
  - the exact command and the commit hash
  - the date and the machine
  - the provider and the model ids, and the Ollama version for a local run
  - whether the machine stayed awake (laptop standby has invalidated runs
    here before)
  - the output itself, or the relevant excerpt
- **Separate measured from inferred:** "measured", "not run" and "inferred" are
  different words; keep them apart.
- **Single eval runs are noisy.** One eval run moves several cases by chance,
  so a difference within the noise band is "flat", not an improvement. Run
  twice when runs are cheap, and report both.

## 5. No secrets in code or commits

- **Never commit `.env` or anything under `benchmarks/results/`.** Never print
  `.env` values into the conversation, logs, docs or commit messages.
- **Keys are read from `.env` only.** Never from the command line, a test, or a
  default in code.
- **Treat these as secrets:**
  - `JWT_SECRET`, `POSTGRES_PASSWORD` and `INTERNAL_TOKEN`
  - `INFERENCE_API_KEY`, `GROQ_API_KEY` and any API key
  - bearer tokens, including from load-test accounts
  - `SEC_EDGAR_USER_AGENT`, which holds a personal email address. In docs and
    code use a placeholder such as `you@yourdomain.com`; in CI, the repository
    secret.
- **Placeholders must not work as values.** Example files (`.env.example`,
  `.env.deploy.example`) take placeholders that fail validation, never values
  that work if copied. A known value that is accepted is a secret everyone has.
- **Check before committing:** run `git diff --cached` and look for keys,
  tokens, passwords, email addresses and `Authorization:` headers. Redact
  tokens from any pasted output.
- **If a secret is ever committed,** stop and tell the user. It needs rotating,
  and rewriting history is their decision.
