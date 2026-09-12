"""Prove a running deployment actually works, from the outside.

    python scripts/verify_deployment.py
    python scripts/verify_deployment.py --base-url http://203.0.113.10:8000

Everything here goes over HTTP as a real client would, so it makes no
assumptions about where the app runs: the host, a container, or a cloud VM. That
is the point -- it is the same check for all three, which is what makes "the
containerized stack behaves like the host one" a measurement rather than a
belief.

Distinct from `scripts/smoke_test.py`, which tests the inference proxy (/v1/*).
This tests the application: signing up, logging in, asking a question, watching
it stream, and confirming another user cannot read the result.

Exits non-zero if any check fails, and says which.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS = "  PASS"
FAIL = "  FAIL"

# A question with a known-good answer in the indexed corpus, phrased so it needs
# a tool. "What is 2+2" would pass while proving nothing about retrieval.
QUESTION = "What was NVIDIA's gross margin in fiscal 2026?"
STREAM_QUESTION = "What was Apple's gross margin in fiscal 2025?"


class Checks:
    """Accumulates results so one failure does not hide the rest."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def ok(self, label: str, detail: str = "") -> None:
        self.passed += 1
        print(f"{PASS}  {label}{'  ' + detail if detail else ''}")

    def bad(self, label: str, detail: str) -> None:
        self.failures.append(f"{label}: {detail}")
        print(f"{FAIL}  {label}  {detail}")

    def check(self, label: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.ok(label, detail)
        else:
            self.bad(label, detail or "condition not met")
        return condition


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def verify(base: str, timeout: float) -> int:
    checks = Checks()
    client = httpx.Client(base_url=base, timeout=timeout)

    # ── liveness and readiness, one dependency at a time ────────────────────
    #
    # Separately, and in this order, because a single aggregate health check
    # cannot tell "the database is down" from "the model is down" -- and those
    # have completely different fixes.
    section("1. health")
    for path, label in (
        ("/health", "process is up"),
        ("/health/db", "Postgres reachable and migrated"),
        ("/health/redis", "Redis reachable"),
        ("/v1/health", "inference upstream reachable"),
    ):
        try:
            response = client.get(path)
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            checks.bad(label, f"{path} -> {type(exc).__name__}: {exc}")
            continue
        checks.check(label, response.status_code == 200,
                     f"{path} -> {response.status_code} {json.dumps(body)[:120]}")
        # A Redis outage silently stops enforcing limits under the default
        # fail-open policy. An unenforced limit nobody can see is worse than a
        # visible outage, so this is surfaced rather than assumed.
        if path == "/health/redis" and response.status_code == 200:
            checks.check("rate limiting is actually enforcing",
                         body.get("limiting") is True,
                         f"limiting={body.get('limiting')} policy={body.get('policy')}")

    try:
        models = client.get("/v1/models").json()
        served = [m.get("id") for m in models.get("data", [])]
        checks.check("inference server is serving models", bool(served), str(served))
    except Exception as exc:  # noqa: BLE001
        checks.bad("inference server is serving models", f"{type(exc).__name__}: {exc}")

    # ── auth ────────────────────────────────────────────────────────────────
    section("2. auth")
    email = f"verify-{uuid.uuid4().hex[:10]}@example.com"
    password = "correct-horse-battery-staple"
    token = ""
    try:
        signup = client.post("/auth/signup", json={"email": email, "password": password})
        checks.check("signup issues a token", signup.status_code == 201,
                     f"{signup.status_code} {signup.text[:100]}")
        token = signup.json().get("access_token", "")
    except Exception as exc:  # noqa: BLE001
        checks.bad("signup issues a token", f"{type(exc).__name__}: {exc}")

    # Logging in separately matters: signup returning a token does not prove the
    # password was stored in a form login can verify.
    try:
        login = client.post("/auth/login", json={"email": email, "password": password})
        checks.check("login with the same credentials works", login.status_code == 200,
                     f"{login.status_code} {login.text[:100]}")
        token = login.json().get("access_token", token)
    except Exception as exc:  # noqa: BLE001
        checks.bad("login with the same credentials works", f"{type(exc).__name__}: {exc}")

    if not token:
        print("\nNo token; cannot continue. Everything below needs one.")
        return report(checks)

    auth = {"Authorization": f"Bearer {token}"}
    me = client.get("/auth/me", headers=auth)
    checks.check("GET /auth/me identifies the caller",
                 me.status_code == 200 and me.json().get("email") == email,
                 f"{me.status_code} {me.text[:100]}")

    checks.check("a forged token is rejected",
                 client.get("/auth/me", headers={"Authorization": "Bearer not.a.token"})
                 .status_code == 401)

    # Present on every response, and the only way a client can pace itself.
    checks.check("rate-limit headers are present",
                 "RateLimit-Remaining" in me.headers,
                 f"remaining={me.headers.get('RateLimit-Remaining')}")

    # ── the agent, buffered ─────────────────────────────────────────────────
    section("3. ask")
    conversation = client.post("/conversations", json={}, headers=auth)
    checks.check("a conversation can be created", conversation.status_code == 201,
                 f"{conversation.status_code} {conversation.text[:100]}")
    if conversation.status_code != 201:
        return report(checks)
    conversation_id = conversation.json()["id"]

    print(f"        asking: {QUESTION}")
    started = time.perf_counter()
    try:
        answer = client.post("/ask", headers=auth, json={
            "conversation_id": conversation_id,
            "message": QUESTION,
            "include_trace": True,
        })
    except Exception as exc:  # noqa: BLE001
        checks.bad("POST /ask returns an answer", f"{type(exc).__name__}: {exc}")
        return report(checks)
    elapsed = time.perf_counter() - started

    checks.check("POST /ask returns 200", answer.status_code == 200,
                 f"{answer.status_code} in {elapsed:.0f}s")
    if answer.status_code == 200:
        body = answer.json()
        print(f"        stop_reason={body.get('stop_reason')} "
              f"completed={body.get('completed')} "
              f"iterations={body.get('iterations')} {elapsed:.0f}s")
        for line in (body.get("answer") or "").splitlines():
            print(f"        | {line}")
        # An answer is never empty, even on failure -- the loop writes a written
        # explanation instead. So emptiness is a real defect, not a slow model.
        checks.check("the answer is not empty", bool((body.get("answer") or "").strip()))
        # The whole point of the system: the answer came from a tool, not recall.
        steps = body.get("steps") or []
        tools_used = [s.get("tool") for s in steps]
        checks.check("at least one tool ran", bool(steps), str(tools_used))
        checks.check("the run completed", body.get("completed") is True,
                     f"stop_reason={body.get('stop_reason')}")

    # ── persistence ─────────────────────────────────────────────────────────
    section("4. the turn was stored")
    messages = client.get(f"/conversations/{conversation_id}/messages", headers=auth)
    stored = messages.json().get("messages", []) if messages.status_code == 200 else []
    checks.check("the conversation has both messages",
                 messages.status_code == 200 and len(stored) >= 2,
                 f"{messages.status_code}, {len(stored)} message(s): "
                 f"{[m.get('role') for m in stored]}")

    # ── the agent, streaming ────────────────────────────────────────────────
    #
    # Checked separately because it is a different code path: SSE through the
    # proxy, and the place a buffering reverse proxy would break things without
    # breaking /ask.
    section("5. ask/stream")
    print(f"        asking: {STREAM_QUESTION}")
    seen: list[str] = []
    tokens = 0
    first_token_at = None
    started = time.perf_counter()
    try:
        with client.stream("POST", "/ask/stream", headers=auth, json={
            "conversation_id": conversation_id,
            "message": STREAM_QUESTION,
        }) as stream:
            checks.check("the stream opens with 200", stream.status_code == 200,
                         str(stream.status_code))
            for line in stream.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:].strip())
                kind = event.get("type")
                if kind == "token":
                    tokens += 1
                    if first_token_at is None:
                        first_token_at = time.perf_counter() - started
                else:
                    seen.append(kind)
                    print(f"        <- {kind}")
                if kind in ("done", "error"):
                    break
    except Exception as exc:  # noqa: BLE001
        checks.bad("the stream completes", f"{type(exc).__name__}: {exc}")

    checks.check("progress events arrived before the answer",
                 "iteration" in seen, f"events={seen}")
    # Tokens arriving one at a time is the difference between streaming and a
    # slow request that returns everything at the end.
    checks.check("the answer streamed as tokens", tokens > 1,
                 f"{tokens} token event(s), first at "
                 f"{first_token_at:.1f}s" if first_token_at else f"{tokens} token(s)")
    checks.check("the stream ended with done", seen and seen[-1] == "done",
                 f"last event={seen[-1] if seen else 'none'}")

    # ── isolation ───────────────────────────────────────────────────────────
    #
    # The one security property worth re-checking from outside on every
    # deployment: a second real user must not reach the first one's data.
    section("6. cross-user isolation")
    other = client.post("/auth/signup", json={
        "email": f"verify-other-{uuid.uuid4().hex[:10]}@example.com",
        "password": password,
    })
    if other.status_code == 201:
        other_auth = {"Authorization": f"Bearer {other.json()['access_token']}"}
        checks.check("another user cannot read this conversation",
                     client.get(f"/conversations/{conversation_id}/messages",
                                headers=other_auth).status_code == 404)
        checks.check("another user cannot stream into it",
                     client.post("/ask/stream", headers=other_auth, json={
                         "conversation_id": conversation_id, "message": "hello",
                     }).status_code == 404)
    else:
        checks.bad("second user could be created", f"{other.status_code}")

    client.close()
    return report(checks)


def report(checks: Checks) -> int:
    print(f"\n{'=' * 70}")
    if checks.failures:
        print(f"FAILED  {len(checks.failures)} of {checks.passed + len(checks.failures)} checks")
        for failure in checks.failures:
            print(f"  - {failure}")
        return 1
    print(f"OK  all {checks.passed} checks passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000",
                        help="where the API is (default: %(default)s)")
    # Generous: a CPU-hosted 7B model takes minutes per answer, and a timeout
    # here would report a working deployment as broken.
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="per-request timeout in seconds (default: %(default)s)")
    args = parser.parse_args()

    print(f"Verifying {args.base_url}")
    return verify(args.base_url.rstrip("/"), args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
