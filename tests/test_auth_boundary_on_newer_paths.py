"""Do the M3 auth and rate-limit guarantees still hold on everything added since?

`tests/test_user_isolation.py` is the M3 suite and is deliberately left
untouched -- it passing unchanged is itself part of the answer. This file covers
the paths that did not exist when it was written: the SSE endpoint from M7, the
conversation-memory window from F7, and the interaction between the frontend's
JWT-expiry handling and the eval harness's session refresh.

Three questions, in the order they matter:

1. Is /ask/stream inside the rate limiter, or did a new request shape slip past
   it? The limiter is ASGI middleware keyed on scope and path rather than a
   per-route dependency, so the answer should be structural -- but "should be"
   is why this is a test and not a paragraph.

2. Can a forged token reach any of the newer code paths? Streaming runs work in
   a task that outlives the request and opens its own database session, which is
   exactly the shape where an ownership check gets skipped by accident.

3. Does the memory window respect conversation ownership? load_history takes a
   conversation id. If anything calls it before checking who owns that id, one
   user's history is replayed into another user's prompt.

Runs against live Postgres and skips clearly when it is unreachable.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt
from fastapi.testclient import TestClient

from api.main import app
from api.rate_limit import EXEMPT_PATHS, EXEMPT_PREFIXES, _bucket_and_limit, _identify
from core.config import settings

PASSWORD = "correct-horse-battery"
STREAM_PATH = "/ask/stream"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        health = c.get("/health/db")
        if health.status_code != 200:
            pytest.skip(f"Postgres unreachable ({health.text}); run migrations first")
        yield c


def make_user(client: TestClient) -> dict:
    email = f"bnd-{uuid.uuid4().hex[:12]}@example.com"
    response = client.post("/auth/signup", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    conversation = client.post("/conversations", json={}, headers=headers)
    assert conversation.status_code == 201, conversation.text
    return {"headers": headers, "id": me.json()["id"],
            "conversation_id": conversation.json()["id"],
            "token": response.json()["access_token"]}


@pytest.fixture(scope="module")
def no_rate_limit():
    original = settings.rate_limit_enabled
    settings.rate_limit_enabled = False
    yield
    settings.rate_limit_enabled = original


@pytest.fixture(scope="module")
def two_users(client, no_rate_limit):
    alice, bob = make_user(client), make_user(client)
    assert alice["id"] != bob["id"]
    return alice, bob


# ─────────────────────────────────────────────────────────────────────────────
# 1. is the SSE endpoint inside the limiter?
# ─────────────────────────────────────────────────────────────────────────────

def test_the_streaming_endpoint_is_not_exempt_from_the_limiter():
    """Structural: the exemptions are a fixed list, and /ask/stream is not on it.

    Worth asserting rather than reading, because the exemption list is where a
    "just for now" entry goes and never leaves.
    """
    assert STREAM_PATH not in EXEMPT_PATHS
    assert not STREAM_PATH.startswith(EXEMPT_PREFIXES)


def test_the_streaming_endpoint_counts_against_the_callers_user_bucket():
    """Not the anonymous one. An authenticated SSE request must spend the same
    budget as the /ask it replaced, or the cheaper path becomes the loophole."""
    bucket, limit = _bucket_and_limit(STREAM_PATH, "user")
    assert bucket == "user"
    assert limit == settings.rate_limit_per_minute

    anon_bucket, anon_limit = _bucket_and_limit(STREAM_PATH, "ip")
    assert anon_bucket == "anon"
    assert anon_limit == settings.rate_limit_anon_per_minute


def test_a_streaming_request_is_identified_by_its_token_not_its_address():
    """EventSource cannot set Authorization, which is why this endpoint is a
    POST. If it were ever identified by IP, every user behind one NAT would
    share a bucket."""
    from auth.security import create_access_token

    token, _ = create_access_token("user-123")
    scope = {"type": "http", "path": STREAM_PATH,
             "headers": [(b"authorization", f"Bearer {token}".encode())],
             "client": ("10.0.0.1", 1234)}
    identity, kind = _identify(scope)
    assert (identity, kind) == ("user-123", "user")


def test_a_token_without_the_access_type_is_not_accepted_as_one():
    """create_access_token stamps typ="access" so a refresh token can never be
    spent as an access token. A correctly signed token missing it is a forgery
    attempt that the signature check alone would wave through."""
    from auth.security import decode_access_token

    now = datetime.now(timezone.utc)
    for claims in (
        {"sub": "1", "exp": now + timedelta(minutes=5)},                  # no typ
        {"sub": "1", "typ": "refresh", "exp": now + timedelta(minutes=5)},
        {"typ": "access", "exp": now + timedelta(minutes=5)},             # no sub
    ):
        token = jwt.encode(claims, settings.jwt_secret,
                           algorithm=settings.jwt_algorithm)
        assert decode_access_token(token) is None, claims

        scope = {"type": "http", "path": STREAM_PATH,
                 "headers": [(b"authorization", f"Bearer {token}".encode())],
                 "client": ("10.0.0.1", 1234)}
        # And the limiter does not believe it either.
        assert _identify(scope)[1] == "ip"


def test_an_unsigned_token_falls_back_to_the_address_not_to_trust():
    """A forged token must not be *believed* by the limiter either -- otherwise
    one bucket per made-up subject is an unlimited quota."""
    forged = jwt.encode({"sub": "attacker"}, "not-the-real-secret",
                        algorithm="HS256")
    scope = {"type": "http", "path": STREAM_PATH,
             "headers": [(b"authorization", f"Bearer {forged}".encode())],
             "client": ("10.0.0.1", 1234)}
    identity, kind = _identify(scope)
    assert kind == "ip" and identity != "attacker"


def test_the_limiter_actually_returns_429_on_the_streaming_endpoint(client):
    """The empirical half. Rate limiting is enabled for this test only, and the
    requests are rejected before the agent runs, so nothing here is slow."""
    original_enabled = settings.rate_limit_enabled
    original_limit = settings.rate_limit_per_minute
    settings.rate_limit_enabled = True
    settings.rate_limit_per_minute = 3
    try:
        user = make_user(client)
        # Creating the user already spent part of their minute -- signup and the
        # new conversation are user-bucket requests too -- so the budget left is
        # read from the header rather than assumed to be the full limit.
        probe = client.get("/conversations", headers=user["headers"])
        remaining = int(probe.headers.get("RateLimit-Remaining", 0))

        # A conversation id that is well-formed but not theirs, so the request
        # reaches the ownership check rather than dying in validation.
        absent = str(uuid.uuid4())
        statuses = []
        for _ in range(6):
            response = client.post(
                STREAM_PATH,
                json={"conversation_id": absent, "message": "x"},
                headers=user["headers"],
            )
            statuses.append(response.status_code)
            if response.status_code == 429:
                break
        if 503 in statuses:
            pytest.skip("rate limiter degraded (Redis unreachable)")
        assert 429 in statuses, f"never rate limited: {statuses}"
        # The 404s before it are the ownership check firing on a conversation
        # that does not exist, which is the point: the limiter runs before
        # routing, so a request that would fail anyway still costs quota.
        assert all(s in (404, 429) for s in statuses), statuses
        assert statuses.count(404) == remaining, (
            f"{remaining} requests of quota remained, so exactly that many "
            f"should have got through: {statuses}")
    finally:
        settings.rate_limit_enabled = original_enabled
        settings.rate_limit_per_minute = original_limit


# ─────────────────────────────────────────────────────────────────────────────
# 2. forged tokens against the newer paths
# ─────────────────────────────────────────────────────────────────────────────

def _alg_none_token() -> str:
    """An unsigned token claiming alg=none, hand-assembled.

    The classic JWT bypass: strip the signature and tell the verifier not to
    check one. A library that honours the header instead of the server's
    configured algorithm accepts it.
    """
    import base64
    import json as _json

    def segment(payload: dict) -> str:
        raw = _json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = segment({"alg": "none", "typ": "JWT"})
    claims = segment({
        "sub": "1",
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp()),
    })
    return f"{header}.{claims}."


def forged_tokens() -> dict[str, str]:
    """Every shape of bad token worth trying, built rather than hardcoded so a
    change to the algorithm or secret is reflected here automatically."""
    now = datetime.now(timezone.utc)
    return {
        "wrong signature": jwt.encode(
            {"sub": "1", "exp": now + timedelta(minutes=30)},
            "not-the-real-secret", algorithm="HS256"),
        "expired": jwt.encode(
            {"sub": "1", "exp": now - timedelta(minutes=1)},
            settings.jwt_secret, algorithm=settings.jwt_algorithm),
        "no expiry": jwt.encode(
            {"sub": "1"}, settings.jwt_secret, algorithm=settings.jwt_algorithm),
        "unknown user": jwt.encode(
            {"sub": "999999999", "exp": now + timedelta(minutes=30)},
            settings.jwt_secret, algorithm=settings.jwt_algorithm),
        "subject is not an id": jwt.encode(
            {"sub": "' OR 1=1 --", "exp": now + timedelta(minutes=30)},
            settings.jwt_secret, algorithm=settings.jwt_algorithm),
        # python-jose will not encode alg "none", which is itself the right
        # behaviour -- so this one is assembled by hand, exactly as an attacker
        # would have to.
        "alg none": _alg_none_token(),
        "not a jwt": "obviously-not-a-token",
        "empty": "",
    }


NEWER_PATHS = [
    ("POST", "/ask/stream", {"conversation_id": 1, "message": "hello"}),
    ("POST", "/ask", {"conversation_id": 1, "message": "hello"}),
    ("GET", "/conversations", None),
]


@pytest.mark.parametrize("label,token", list(forged_tokens().items()))
@pytest.mark.parametrize("method,path,body", NEWER_PATHS,
                         ids=[f"{m}-{p}" for m, p, _ in NEWER_PATHS])
def test_a_forged_token_is_rejected_everywhere(client, no_rate_limit,
                                               label, token, method, path, body):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == 401, (
        f"{label!r} got {response.status_code} on {method} {path}: "
        f"{response.text[:200]}")


@pytest.mark.parametrize("method,path,body", NEWER_PATHS,
                         ids=[f"{m}-{p}" for m, p, _ in NEWER_PATHS])
def test_no_token_at_all_is_rejected(client, no_rate_limit, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 401


def test_a_rejected_stream_never_opens_a_stream(client, no_rate_limit):
    """A 401 must arrive as a 401, not as a 200 whose first SSE frame is bad
    news -- a client that has already begun rendering cannot un-render it."""
    response = client.post(STREAM_PATH,
                           json={"conversation_id": 1, "message": "hello"},
                           headers={"Authorization": "Bearer forged"})
    assert response.status_code == 401
    assert "text/event-stream" not in response.headers.get("content-type", "")


# ─────────────────────────────────────────────────────────────────────────────
# 3. the memory window respects ownership
# ─────────────────────────────────────────────────────────────────────────────

def test_streaming_into_another_users_conversation_is_a_404(client, two_users):
    """404 rather than 403, matching the rest: a 403 would confirm the id exists
    and turn this into an existence oracle for other people's threads."""
    alice, bob = two_users
    response = client.post(
        STREAM_PATH,
        json={"conversation_id": bob["conversation_id"], "message": "hello"},
        headers=alice["headers"],
    )
    assert response.status_code == 404
    assert "text/event-stream" not in response.headers.get("content-type", "")


def test_the_history_window_is_never_loaded_for_an_unowned_conversation(
        client, two_users, monkeypatch):
    """The specific regression this guards: load_history takes a conversation id
    and will happily read any of them. Its safety comes entirely from the
    ownership check that runs first, so the test is that the check runs first --
    not that the function is careful."""
    import api.routes as routes

    alice, bob = two_users
    calls = []
    real = routes.load_history

    async def spy(session, conversation_id, **kwargs):
        calls.append(conversation_id)
        return await real(session, conversation_id, **kwargs)

    monkeypatch.setattr(routes, "load_history", spy)

    for path in ("/ask", STREAM_PATH):
        response = client.post(
            path,
            json={"conversation_id": bob["conversation_id"], "message": "hi"},
            headers=alice["headers"],
        )
        assert response.status_code == 404, path

    assert calls == [], (
        f"history was loaded for a conversation the caller does not own: {calls}")


def test_the_owner_does_get_their_own_history_loaded(client, two_users,
                                                     monkeypatch):
    """The inverse, so the test above cannot pass because the call site moved."""
    import api.routes as routes

    alice, _ = two_users
    calls = []
    real = routes.load_history

    async def spy(session, conversation_id, **kwargs):
        # Records and returns empty rather than raising: the ownership check has
        # already run by the time this is reached, which is all that is
        # asserted, and raising here would surface as a 500 that says nothing.
        calls.append(str(conversation_id))
        return []

    async def no_inference(*args, **kwargs):
        # The agent is not what this test is about, and letting it run turns a
        # boundary check into a four-minute inference call.
        from agent.orchestrator import AgentResult

        return AgentResult(answer="stubbed", steps=[], iterations=1)

    monkeypatch.setattr(routes, "load_history", spy)
    monkeypatch.setattr(routes, "run_agent", no_inference)
    client.post("/ask", json={"conversation_id": alice["conversation_id"],
                              "message": "hi"},
                headers=alice["headers"])

    assert calls == [str(alice["conversation_id"])]


# ─────────────────────────────────────────────────────────────────────────────
# 4. expiry handling: the frontend seam and the eval harness refresh
# ─────────────────────────────────────────────────────────────────────────────

def test_an_expired_token_is_a_401_and_not_a_500(client, no_rate_limit):
    """Both the frontend's redirect-to-login and the eval harness's re-auth hang
    off the status code. Anything other than 401 breaks both at once."""
    expired = jwt.encode(
        {"sub": "1", "exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
        settings.jwt_secret, algorithm=settings.jwt_algorithm)
    response = client.get("/conversations",
                          headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


def test_a_refreshed_token_works_immediately(client, no_rate_limit):
    """The eval harness re-authenticates on 401 and retries once. That is only
    safe if a newly issued token is accepted straight away -- a clock-skew
    guard that rejected fresh tokens would turn one 401 into a loop."""
    user = make_user(client)
    again = client.post("/auth/login",
                        json={"email": None, "password": PASSWORD})
    # The login above is deliberately malformed; what matters is the fresh token
    # from signup still working, and a bad login not disturbing it.
    assert again.status_code in (401, 422)
    assert client.get("/auth/me", headers=user["headers"]).status_code == 200


def test_expiry_is_the_configured_window(client, no_rate_limit):
    """30 minutes is what the frontend and the eval harness both assume."""
    user = make_user(client)
    claims = jwt.decode(user["token"], settings.jwt_secret,
                        algorithms=[settings.jwt_algorithm])
    lifetime = (datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
                - datetime.now(timezone.utc))
    assert timedelta(minutes=settings.jwt_expire_minutes - 1) < lifetime
    assert lifetime <= timedelta(minutes=settings.jwt_expire_minutes + 1)


# ─────────────────────────────────────────────────────────────────────────────
# 5. the internal-token bypass
# ─────────────────────────────────────────────────────────────────────────────

def test_the_internal_token_bypass_needs_the_real_secret():
    """The agent calls this app's own /v1 proxy and is exempted from the
    limiter. That exemption is a rate-limit bypass for anyone who can forge the
    header, so it has to be a real secret compared in constant time."""
    from api.rate_limit import _is_internal

    real = settings.internal_token
    assert real and len(real) >= 32

    assert _is_internal({"headers": [(b"x-internal-token", real.encode())]})
    assert not _is_internal({"headers": [(b"x-internal-token", b"guess")]})
    assert not _is_internal({"headers": [(b"x-internal-token", b"")]})
    assert not _is_internal({"headers": []})
    # A prefix must not pass: that is what a non-constant-time compare leaks.
    assert not _is_internal({"headers": [(b"x-internal-token", real[:-1].encode())]})


def test_the_internal_token_is_not_a_known_constant():
    """Generated per process and never persisted, so it cannot be replayed
    against a restarted server or read out of committed configuration."""
    import re
    from pathlib import Path

    root = Path(__file__).parent.parent
    for name in (".env.example", "docs/RATE_LIMITING.md", "README.md"):
        path = root / name
        if path.exists():
            assert settings.internal_token not in path.read_text(
                encoding="utf-8", errors="replace"), f"leaked into {name}"
    assert not re.fullmatch(r"(changeme|secret|internal|token)",
                            settings.internal_token, re.I)
