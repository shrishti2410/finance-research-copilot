# CI: a test gate and an eval gate, and how to roll back

**Status:** written and validated locally (YAML parses, the skip gate is unit
tested, and its commands are the ones that run on the dev host). **It has not run
on GitHub.** This repository has no remote yet. The first push is the first real
run; see [before the first push](#before-the-first-push).

This is the free, always-available part of CI/CD: every change is tested and,
where it can change the agent's behaviour, evaluated. There is no deployment
automation, because there is no persistent server to deploy to.

## What runs, and when

| workflow | trigger | fails the build when |
|---|---|---|
| [`tests.yml`](../.github/workflows/tests.yml) | every push and pull request | a test fails, **any test is skipped**, fewer tests ran than the floor, or the safety run is under its own floor |
| [`eval-gate.yml`](../.github/workflows/eval-gate.yml) | pushes and pull requests touching `agent/`, `rag/`, `tools/`, `eval/`, or the CI files | accuracy is **worse than `eval/baselines/current.json`** beyond the noise band, or the eval could not run at all |

Both prepare the same stack through
[`.github/actions/prepare-stack`](../.github/actions/prepare-stack/action.yml):
- **Services:** Postgres with pgvector and Redis, as service containers.
- **Python:** CPU-only torch plus `requirements.txt`.
- **Ollama:** pinned to 0.34.0, with `qwen2.5:7b` and `qwen2.5:1.5b` pulled.
- **Settings:** the same Ollama settings as `docker-compose.yml`'s CPU defaults.
- **Data:** migrations, then the NVDA and AAPL 10-Ks indexed.

The models, Hugging Face weights and EDGAR downloads are cached between runs.

`eval/` is in the eval gate's paths as well as the three directories named in
the requirement. A change to the scorer or to the baseline moves the gate just as
much as a change to the agent does.

## The test gate: why a skip fails the build

Every external dependency this suite has becomes a **skip** when it is missing,
not a failure:
- Postgres, Redis and Ollama
- bge-small and the Qwen tokenizer
- the cached filings and the index

On a laptop that is the right behaviour. On a CI runner it would turn a Postgres
service that never started into a green build that tested none of the database,
auth, isolation or pool-exhaustion code. So
[`scripts/ci_check_junit.py`](../scripts/ci_check_junit.py) reads pytest's JUnit
reports and fails on:

- **any skip.** It names each skipped test and the reason pytest recorded, which
  is almost always the dependency that did not come up.
- **fewer tests than `--min-tests`.** This catches the other way to test nothing:
  tests that stopped being collected leave no skip behind. The floor sits just
  under the current count. Removing tests on purpose means lowering it in the
  same change.
- **a safety run under `--min-safety`.** `pytest -m safety` runs as its own step,
  first, so a violated safety property shows up as its own red check and not as
  one failure among 900. `pyproject.toml` explains why these are release
  blockers rather than ordinary tests.

## The eval gate: what "drops below the baseline" means

The gate runs `python -m eval.compare`. That runs the 40-question eval over HTTP
against a live API on the runner, rescores both the run and the baseline with
today's `eval/metrics.py`, and exits:

| exit | verdict | build |
|---|---|---|
| 0 | better, or flat within the band | passes |
| 1 | **worse**: net cases lost exceed `EVAL_MIN_CASES` | **fails** |
| 2 | the eval could not run, or produced no cases | **fails** |

**The gate fails on a drop beyond the noise band, not on any drop at all.** This
is a deliberate reading of "fail if accuracy drops below the baseline", and the
reason is measured. The eval is not deterministic: two runs of *unchanged* code
scored 38.9% and 33.3% over the same 18 cases and disagreed on five of them
(`eval/baselines/README.md`). With 40 cases, one case is 2.5 points. A gate on
any net loss would fail commits that changed nothing, and a gate that fails on
noise gets ignored or disabled.

`EVAL_MIN_CASES` (default 2, set in `eval-gate.yml`) is the band:
- **Lowering it** makes the gate stricter and flakier.
- **Raising it** lets real regressions through.

Change it only with evidence from repeated runs of an unchanged commit.

Even when the verdict is flat, the run page shows every case that moved in each
direction, and the per-case report is kept as the `eval-report` artifact. Two
cases broken and two unrelated cases fixed is flat by the number, and still
worth reading.

**What the eval does not gate.** It measures accuracy only:
- **Groundedness** is `eval/judge.py`, not run in CI.
- **Retrieval recall** is `eval/retrieval_eval.py`, not run in CI.
- **Safety** is the test gate's `-m safety` step.

A change that raised accuracy by inventing more unsourced figures would pass this
gate.

### Known risks, stated before they happen

- **The baseline was recorded on the dev laptop, not on a runner.** llama.cpp's
  output can differ between CPUs with different instruction sets, even at
  temperature 0. If unchanged commits keep landing outside the band on CI,
  re-baseline from a CI run instead of widening the band:
  1. Download `eval_run.json` from a green run of an unchanged commit.
  2. Run `python -m eval.compare --promote eval_run.json --name ci-runner --note "..."`.
  3. Open a pull request with the new `eval/baselines/current.json`. It triggers
     the gate itself.
- **Live data moves.** Tools read live prices and news from Yahoo Finance and
  Google News. Stock-price answers change between runs, and cloud IP ranges can
  be rate-limited. A tool that fails on the runner shows up as a worse eval, and
  the per-case report shows which tool.
- **Runner size.** At the time of writing, GitHub's standard hosted Linux runner
  has 4 vCPUs and 16 GB of RAM for public repositories, and 2 vCPUs and 7 GB for
  private ones. The two models need about 6.7 GB before the API, Postgres and
  torch. **On a private repository both workflows are expected to run out of
  memory on the standard runner** and need a larger or self-hosted runner.
  Check GitHub's current runner specifications before relying on either figure.
- **Duration.** A full eval took about 100 minutes on the dev laptop. The job
  timeout is 330 minutes, inside the hosted runner's 6-hour limit.
- **The live injection test has no time bound of its own.**
  `test_injection::test_the_live_model_does_not_obey_an_injected_filing` runs the
  real agent against Ollama. When CI was validated locally, it took **54
  minutes**, against about 5 for the whole suite normally.
  - **What was slow:** one 7b call spent those 54 minutes on a single 1,024-token
    prompt batch, which normally takes 30–50 seconds. The runner then logged
    `stop processing: n_tokens = 1029`. That pattern fits the model's weights
    being paged out under memory pressure: 1.4 GiB was free when the second model
    loaded, on a laptop also running an IDE and a browser.
  - **Not established:** why the agent's 300 s read timeout did not end the call.
    The orchestrator turns any failed call into `inference_error` immediately, so
    a timeout that fired would have ended the test after about 5 minutes. This is
    an open bug, and it is recorded here so it is not rediscovered.
  - **Reproduced through the full stack.** The `NUM_PARALLEL` load test held five
    requests for 3 h 53 min each, and another five for 25 minutes, all under heavy
    paging. The evidence and the leading hypothesis are in
    `docs/INFERENCE_REPLICAS.md`: httpx's read timeout limits silence between
    bytes, not total call time.
  - **In CI:** the job's `timeout-minutes` is the only bound. A standard public
    runner has nothing else competing for its 16 GB, so paging on this scale is
    not expected there.

## Before the first push

1. **Create the repository and add the secret.** In the repository settings, add
   `SEC_EDGAR_USER_AGENT` under *Secrets and variables → Actions*, with a name
   and a contact SEC can reach, e.g. `finance-research-copilot/0.1 (you@yourdomain)`.
   It is only ever read from the secret, never written into a workflow file.
   Pull requests from forks do not receive secrets, so on those the stack
   preparation fails and says why. That is intended: an index built without a
   valid User-Agent is not something to test against.
2. **Expect the first run to be slow.** Nothing is cached yet. The models alone
   are about 5.7 GB.
3. **Watch the first eval-gate run's verdict against the laptop baseline.** It is
   the evidence for or against the first known risk above.

## Rollback: returning to the last known-good commit

No automation, by design. This is the procedure, and **the eval baseline is the
signal**.

### 1. Decide that it regressed

A deploy has regressed when either of these holds:

- **The eval gate went red on `main`.** A pull request should have caught it
  first; a direct push, or a regression that only appears in combination with
  another change, will not have been caught.
- **The deployed stack is worse than the baseline.** `run_eval` works against any
  API: it signs up a throwaway account and asks the 40 questions.

  ```bash
  python -m eval.compare --api https://<deployed-host>
  ```

  Exit code 1 ("worse") is the rollback signal. Flat is not, even if the number
  dipped: that is the same noise band the gate uses.

A red test gate on `main` is also a signal. It is usually a bug to fix forward,
unless it is a safety test: those are release blockers, so roll back.

### 2. Find the last known-good commit

Known-good means **both workflows passed, on a commit whose agent, retrieval,
tools and eval code the eval gate actually measured.** The eval gate only runs
when those paths change, so the newest green eval run marks the last measured
behaviour:

```bash
good=$(gh run list --workflow eval-gate.yml --branch main --status success \
         --limit 1 --json headSha --jq '.[0].headSha')

# the test gate must also have passed at that commit
gh run list --workflow tests.yml --commit "$good" --status success

# what would be undone
git log --oneline "$good"..HEAD
```

A later commit can be more recent and still known-good, if two things hold:
- nothing under `agent/`, `rag/`, `tools/` or `eval/` changed between `$good` and
  that commit (`git diff --stat "$good" <commit> -- agent rag tools eval` is
  empty)
- its own test gate passed

Prefer it: it keeps unrelated fixes.

### 3. Revert: new commits, never a rewritten `main`

```bash
git switch main && git pull
git revert --no-edit "$good"..HEAD      # one revert commit per undone commit
git push
```

`git revert` keeps history intact, so the regression stays on record, the
revert can itself be reverted once the fix is ready, and nobody's clone breaks.
Do not `git reset --hard` and force-push `main`.

The push triggers both gates on the revert. **The rollback is confirmed when the
eval gate reports flat or better against the same baseline.** Do not promote a
new baseline during a rollback: the baseline is the fixed point being returned to.

### 4. Database migrations first, if any changed

```bash
git diff --name-only "$good" HEAD -- migrations/
```

If that is empty, skip this step. Otherwise the database schema is ahead of the
code being restored, so:

1. Back up: `docker compose exec postgres pg_dump -U postgres finance_copilot > pre-rollback.sql`.
2. Find the revision the good commit expects: `git worktree add ../good "$good"`,
   then `alembic heads` inside `../good`.
3. Downgrade **before** deploying the old code:
   `docker compose run --rm backend alembic downgrade <that revision>`.

A downgrade that drops a column or table destroys its data, which is why the
backup comes first. If a downgrade would lose data you need, fixing forward is the
safer rollback.

### 5. Redeploy and verify

```bash
git pull
docker compose build backend frontend
docker compose up -d
python scripts/verify_deployment.py --base-url https://<deployed-host>
python -m eval.compare --api https://<deployed-host>     # expect flat or better
```

The last line is the same check that triggered the rollback, now expected to
pass. Until it does, the rollback is not finished.
