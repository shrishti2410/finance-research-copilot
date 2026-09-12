"""Load test the whole stack: auth, agent loop, tools, database, inference.

    python benchmarks/provision_users.py --users 20      # once, first
    locust -f benchmarks/locustfile.py --headless -u 5 -r 5 -t 20m \
           --host http://127.0.0.1:8000 --csv benchmarks/results/c05

Milestone 2's `scripts/load_test.py` measured the inference proxy alone -- N
streaming completions, TTFT and TPOT. This measures what a user actually waits
for, which is a different quantity by an order of magnitude: one `/ask/stream` is
several inference calls plus tool execution plus two database writes, and on this
CPU host it is minutes rather than seconds.

Read `benchmarks/README.md` before quoting any number from this. The short
version: the inference server barely batches (measured: 1.15x system throughput
at 4x concurrency), so this is largely a measurement of queueing, and flat
throughput is the expected result rather than a bug.

## Accounts come from a pool, not from signup

`/auth/*` is limited to 10 requests per minute per IP regardless of token,
because signup and login are the brute-force surface. Every simulated user is
127.0.0.1, so signing up inside the test would measure the rate limiter instead
of the agent. `provision_users.py` creates them beforehand; this file only reads
them. Each user also keeps its own conversation, so the authenticated 60/min
limit is per-user and never binds at these rates.
"""

from __future__ import annotations

import itertools
import json
import pathlib
import random
import threading
import time

from locust import HttpUser, constant, events, task
from locust.exception import StopUser

USERS_FILE = pathlib.Path(__file__).parent / "results" / "users.json"

# Questions that genuinely need a tool call, across both indexed tickers. One
# answerable from memory would skip the part of the system being loaded, and
# asking the same one every time would let the model's own cache flatter the
# result.
QUESTIONS = [
    "What was NVIDIA's gross margin in fiscal 2026?",
    "What was Apple's gross margin in fiscal 2025?",
    "What was NVIDIA's operating margin in fiscal 2026?",
    "What was Apple's revenue in fiscal 2025?",
    "What was NVIDIA's revenue in fiscal 2026?",
    "What is NVIDIA's net margin for fiscal 2026?",
]

_accounts: list[dict] = []
_pool = None
_lock = threading.Lock()

# Stop reasons that mean the deployment failed, as opposed to the agent declining
# to answer. `inference_error` is the one this test is hunting: it is what a
# request looks like when it waited past the 300s inference read timeout for a
# slot. `no-done-event` means the stream ended without a terminal frame.
#
# Deliberately NOT here: unverified_figures (the grounding guard), investment
# advice (the advice guard), budget_exhausted and max_iterations. Those are the
# agent working as designed, at any load.
INFRASTRUCTURE_FAILURES = {"inference_error", "no-done-event"}


@events.test_start.add_listener
def load_accounts(environment, **_):
    global _accounts, _pool
    if not USERS_FILE.exists():
        raise SystemExit(
            f"No account pool at {USERS_FILE}.\n"
            f"Run first:  python benchmarks/provision_users.py --users 20"
        )
    _accounts = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    _pool = itertools.cycle(_accounts)

    target = getattr(environment.runner, "target_user_count", None) or "?"
    print(f"\nload test -> {environment.host}")
    print(f"users={target}  account pool={len(_accounts)}")
    if isinstance(target, int) and target > len(_accounts):
        print(f"WARNING: {target} users sharing {len(_accounts)} accounts -- the "
              f"authenticated 60/min limit may bind. Provision more.")
    print("one turn is minutes on a CPU host; expect few samples per user.\n")


class ResearchUser(HttpUser):
    """One person asking research questions back to back."""

    # No think time. This is a saturation test: the question is how the system
    # behaves when asked for more than it can deliver, and a pause between
    # requests would lower the offered load without changing that answer.
    wait_time = constant(0)

    def on_start(self) -> None:
        with _lock:
            account = next(_pool)
        self.headers = {"Authorization": f"Bearer {account['token']}"}
        self.conversation_id = account["conversation_id"]

    @task
    def ask(self) -> None:
        """One full agent turn, streamed, timed as the user experiences it.

        The streaming endpoint rather than /ask, for two reasons: it is what the
        frontend uses, and it separates time-to-first-token from total latency.
        Under queueing those two diverge sharply and a single total would hide
        where the time went.
        """
        question = random.choice(QUESTIONS)
        started = time.perf_counter()
        ttft = None
        tokens = 0
        stop_reason = "no-done-event"
        completed = False

        # NOTE on what Locust records for this call.
        #
        # With stream=True, the built-in timer stops when response *headers*
        # arrive -- not when the stream ends. Measured at concurrency 1 it
        # reported 31 ms for turns that took over three minutes. That is not a
        # useless number (it is the auth and ownership check, which happens
        # before the agent starts, so a 404 for someone else's conversation is
        # still fast under load) but it is not latency, and it is named
        # accordingly. Total and TTFT are fired by hand below.
        with self.client.post(
            "/ask/stream",
            json={"conversation_id": self.conversation_id, "message": question},
            headers=self.headers,
            name="POST /ask/stream (to headers only)",
            stream=True,
            catch_response=True,
        ) as response:
            # A 401 or 429 means the run is no longer measuring the agent, and
            # continuing produces a plausible-looking but worthless result. The
            # first attempt at this sweep died exactly here: tokens expired
            # mid-run, every request became anonymous, and the harness generated
            # ~70,000 429s against the per-IP bucket while the latency table
            # still looked reasonable. So stop the user instead of looping.
            if response.status_code in (401, 429):
                response.failure(f"{response.status_code}: {response.text[:120]}")
                raise StopUser(
                    f"ABORTING: got {response.status_code}. "
                    f"{'Token expired -- run provision_users.py --refresh' if response.status_code == 401 else 'Rate limited -- the pool is too small or tokens are invalid'}"
                )
            if response.status_code != 200:
                response.failure(f"{response.status_code}: {response.text[:160]}")
                return
            try:
                for line in response.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:].strip())
                    kind = event.get("type")
                    if kind == "token":
                        tokens += 1
                        if ttft is None:
                            ttft = time.perf_counter() - started
                    elif kind == "done":
                        stop_reason = event.get("stop_reason", "done")
                        completed = bool(event.get("completed"))
                        break
                    elif kind == "error":
                        response.failure(
                            f"error event: {event.get('error')}: "
                            f"{str(event.get('message'))[:120]}")
                        return
            except Exception as exc:  # noqa: BLE001
                response.failure(f"stream broke: {type(exc).__name__}: {exc}")
                return

            # A 200 that produced no answer is a failure the HTTP status cannot
            # express, and `inference_error` is exactly what a request queued
            # past the 300s read timeout looks like -- the degradation this test
            # exists to find. Recording it as a success would hide the finding.
            total = time.perf_counter() - started

            # ── what counts as a failure, and what does not ──────────────────
            #
            # `completed=False` is the wrong test, and using it made the first
            # smoke run report a 50% failure rate for a system that was working:
            # the guard had refused to ship figures it could not trace to a tool
            # result, which is the behaviour the project exists to have.
            #
            # So only infrastructure failures count. Guard outcomes are recorded
            # by name instead -- they are not errors, but they are worth watching,
            # because a rise in `unverified_figures` under load would itself be a
            # load effect (a longer queue means a longer prompt means truncated
            # evidence) and hiding it would lose that.
            if stop_reason in INFRASTRUCTURE_FAILURES:
                response.failure(f"stop_reason={stop_reason}")
                fire("ask/stream: TOTAL", total, tokens, f"stop_reason={stop_reason}")
                fire(f"outcome: {stop_reason}", 0.0, 0)
                return

            response.success()
            fire(f"outcome: {stop_reason}", 0.0, tokens)

        # The three numbers that matter, fired by hand because the built-in timer
        # above stops at the headers. TOTAL is what a user waits; TTFT is when
        # text starts appearing. The pair is what shows queueing -- if TOTAL and
        # TTFT rise together, time is being spent waiting for a slot rather than
        # generating.
        fire("ask/stream: TOTAL", total, tokens)
        if ttft is not None:
            fire("ask/stream: first token", ttft, tokens)


def fire(name: str, seconds: float, tokens: int, error: str | None = None) -> None:
    events.request.fire(
        request_type="STREAM",
        name=name,
        response_time=seconds * 1000,
        response_length=tokens,
        exception=RuntimeError(error) if error else None,
        context={},
    )
