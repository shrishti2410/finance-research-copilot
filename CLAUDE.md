# CLAUDE.md

Finance Research Copilot is a FastAPI and agent-loop backend with a Next.js client.
It answers questions about public companies from SEC 10-K filings (RAG) and live
tools. The model is reached through an OpenAI-compatible proxy: Qwen on Ollama
locally, or a hosted provider set in `.env`. Start with `docs/PROJECT.md`.
`frontend/CLAUDE.md` adds rules for the Next.js code.

## Current plan (set 2026-11-16)

Speed matters. Ship a minimal version 1, then make version 2 the priority.

**Version 1, minimal.** Only these, in this order where they depend on each other:
- merge the calculator branch after its full hosted eval (scheduled 2026-11-17);
- deploy the public demo (docs/DEPLOY.md; Vercel, Render, Neon, Upstash);
- finish the 15-company ingestion (`feat/step4-corpus`) and make pgvector the
  default database;
- add 20 eval questions for the new companies, then one hosted eval run
  (eval questions still need approval: see "When to stop for approval");
- finish the README (`docs/readme-hiring`).

**Dropped for now:** the 100-question set, hybrid search and re-ranking work,
the hosted noise repeat, and the final Qwen run. Do not start them.

**Version 2, in parallel on its own branch.** It needs no Groq tokens.
- Step 7A: market data (daily prices cached in Postgres behind a provider
  interface), indicators, price analytics, a chart page, and charts returned by
  the agent and rendered in the chat.
- Step 7B: a strategy library and a backtester, with an equity-curve and trade
  page.

**How to split the time.** Work on version 1 whenever it can move: when the user
is available for a step that needs them, or when the Groq daily allowance is
there for a hosted run. Work on version 2 the rest of the time.

## Backlog after version 2 is deployed (set 2026-11-17)

A hardening phase, in this order. Each item is tests-first and ends with a
short report. Do not start it before version 2 is live. Items 1 and 2 bring
back the hybrid search and the larger question set dropped above.

1. **Hybrid retrieval.**
   - Postgres full-text (BM25-style ranking) alongside pgvector, fused with
     Reciprocal Rank Fusion, plus metadata filters.
   - No cross-encoder: the free tier's CPU cannot afford it.
   - Measure on the retrieval check and on the dev eval.
2. **Held-out eval.**
   - Keep the 40 questions as the dev set.
   - Write about 60 new held-out questions: unanswerable, multi-hop,
     cross-company, multi-year and unit traps. Freeze them before any tuning.
   - Report accuracy with 95% Wilson intervals, and abstention accuracy on the
     unanswerable ones.
   - Run the held-out set only once per change set, within the Groq budget.
   - Include cross-company questions ("compare NVIDIA and Apple"). Retrieval's
     company filter (`RETRIEVAL_INFER_COMPANY`) applies only when a question
     names exactly one company, and these questions check it never hurts the
     ones that name two. Hybrid retrieval and that filter stay off until this
     set decides (2026-11-17).
   - New eval questions need approval (see "When to stop for approval").
3. **Claim-level verification.**
   - For each figure in an answer, store the claim, value, unit, type
     (extracted, derived or interpretation), source id (accession, section),
     and the formula for derived ones.
   - Builds on the unit-aware grounding fix. It is a grounding-guard change, so
     show the diff and wait.
   - Frontend: clicking a figure opens its source passage.
4. **Ensemble robustness.**
   - A minimum trade count, shrinkage toward equal weights, and a weight cap.
   - Compare against an equal-weight ensemble, and add the Deflated Sharpe
     Ratio.
   - Evaluate on the 10 hold-out stocks, with the adoption rule written down
     and committed before running.
5. **Strategy drift detection.**
   - Rolling Sharpe against each strategy's own history, with CUSUM.
   - Flag and down-weight drifting strategies, and show it on the signal page.
6. **NVDA, AAPL and MSFT in the signals watchlist**, so one stock page has both
   10-K research and signals.
7. **Light observability and Greeks.**
   - Per-stage timings, tokens, p50 and p95, on a small System page.
   - Greeks (delta, gamma, theta, vega) on the options tab.

**Out of scope for now:** Celery, a full Grafana stack, a portfolio engine,
paper trading, live option chains.

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
