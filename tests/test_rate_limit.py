"""Tests for the Redis rate limiter.

Backed by fakeredis with Lua support, so the sliding-window script is really
executed rather than mocked out. Requires no Redis server and no database.

    pip install "fakeredis[lua]"
    pytest tests/test_rate_limit.py

The middleware is mounted on a purpose-built app rather than `api.main.app`.
Bucket selection and exemption are path-based, so a few stub routes reproduce
them exactly, without every request also dialling a dead Postgres.

What this cannot cover: Redis's own atomicity under genuine parallelism.
fakeredis runs the script in-process, so `test_no_check_then_act_race` proves the
script's logic never double-admits, not that a real Redis serializes it. The
latter is a property of Redis (single-threaded command execution), which is the
reason the logic lives in a script at all.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from fakeredis import aioredis as fakeredis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from api.rate_limit import RateLimiter, RateLimitMiddleware
from auth.security import create_access_token
from core.config import settings

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"

USER_LIMIT = 8
ANON_LIMIT = 5
AUTH_LIMIT = 3


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)
    monkeypatch.setattr(settings, "rate_limit_per_minute", USER_LIMIT)
    monkeypatch.setattr(settings, "rate_limit_anon_per_minute", ANON_LIMIT)
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", AUTH_LIMIT)
    monkeypatch.setattr(settings, "rate_limit_fail_open", True)
    monkeypatch.setattr(settings, "rate_limit_trust_proxy", False)


@pytest.fixture
def limiter():
    return RateLimiter(client=fakeredis.FakeRedis())


@pytest.fixture
def client(limiter):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await limiter.connect()
        yield
        await limiter.close()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(RateLimitMiddleware, limiter=limiter)

    @app.get("/probe")
    async def probe():
        return {"ok": True}

    @app.get("/auth/me")
    async def auth_me():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    with TestClient(app) as c:
        yield c


def token(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id)[0]}"}


def raises_outage():
    async def boom(*args, **kwargs):
        raise RedisConnectionError("simulated outage")

    return boom


# ── enforcement ──────────────────────────────────────────────────────────────

def test_anonymous_limit_enforced(client):
    codes = [client.get("/probe").status_code for _ in range(ANON_LIMIT + 2)]
    assert codes[:ANON_LIMIT] == [200] * ANON_LIMIT
    assert codes[ANON_LIMIT:] == [429, 429]


def test_429_tells_the_client_how_to_back_off(client):
    for _ in range(ANON_LIMIT + 1):
        response = client.get("/probe")
    assert response.status_code == 429
    # Without Retry-After the usual client reaction to a 429 is an immediate
    # retry, which turns the limiter into a hot loop.
    assert int(response.headers["retry-after"]) > 0
    assert response.headers["ratelimit-limit"] == str(ANON_LIMIT)
    assert response.json()["retry_after"] > 0


def test_headers_count_down_on_allowed_responses(client):
    first = client.get("/probe")
    second = client.get("/probe")
    assert first.headers["ratelimit-limit"] == str(ANON_LIMIT)
    assert first.headers["x-ratelimit-limit"] == str(ANON_LIMIT)
    assert int(first.headers["ratelimit-remaining"]) == ANON_LIMIT - 1
    assert int(second.headers["ratelimit-remaining"]) == ANON_LIMIT - 2
    assert int(first.headers["ratelimit-reset"]) > 0


# ── who gets counted against whom ────────────────────────────────────────────

def test_users_have_separate_buckets(client):
    codes = [client.get("/probe", headers=token(ALICE)).status_code for _ in range(USER_LIMIT + 1)]
    assert codes[-1] == 429

    bob = client.get("/probe", headers=token(BOB))
    assert bob.status_code == 200
    assert bob.headers["ratelimit-limit"] == str(USER_LIMIT)


def test_authenticated_and_anonymous_buckets_are_distinct(client):
    for _ in range(USER_LIMIT + 1):
        client.get("/probe", headers=token(ALICE))
    anonymous = client.get("/probe")
    assert anonymous.status_code == 200
    assert anonymous.headers["ratelimit-limit"] == str(ANON_LIMIT)


def test_auth_endpoints_have_their_own_stricter_bucket(client):
    codes = [client.get("/auth/me").status_code for _ in range(AUTH_LIMIT + 1)]
    assert codes[-1] == 429
    # Exhausting the login budget must not lock the caller out of everything else.
    assert client.get("/probe").status_code == 200


def test_auth_bucket_is_ip_keyed_even_with_a_token(client):
    """A valid token must not buy a bigger budget on the credential endpoints."""
    codes = [
        client.get("/auth/me", headers=token(ALICE)).status_code for _ in range(AUTH_LIMIT + 1)
    ]
    assert codes[-1] == 429


# ── exemptions ───────────────────────────────────────────────────────────────

def test_health_is_never_limited(client):
    # An orchestrator polling /health must not be able to 429 itself into a
    # restart loop.
    codes = {client.get("/health").status_code for _ in range(ANON_LIMIT * 4)}
    assert codes == {200}
    assert "ratelimit-limit" not in client.get("/health").headers
    # ...and those polls must not have spent the caller's real budget either.
    assert int(client.get("/probe").headers["ratelimit-remaining"]) == ANON_LIMIT - 1


# ── window behaviour ─────────────────────────────────────────────────────────

def test_window_slides(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 1)
    for _ in range(ANON_LIMIT + 1):
        last = client.get("/probe")
    assert last.status_code == 429

    import time

    time.sleep(1.1)
    recovered = client.get("/probe")
    assert recovered.status_code == 200
    assert int(recovered.headers["ratelimit-remaining"]) == ANON_LIMIT - 1


def test_no_check_then_act_race(limiter):
    """50 simultaneous requests against a limit of 10 must admit exactly 10.

    A client-side ZCARD-then-ZADD would let many of them read a count below the
    limit before any of them wrote, and admit far more than 10.
    """

    async def run():
        await limiter.connect()
        decisions = await asyncio.gather(*(limiter.check("user", "racer", 10) for _ in range(50)))
        await limiter.close()
        return sum(1 for d in decisions if d.allowed)

    assert asyncio.run(run()) == 10


# ── degraded Redis ───────────────────────────────────────────────────────────

def test_fail_open_serves_requests_when_redis_is_down(client, limiter, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_fail_open", True)
    limiter._script = raises_outage()

    codes = [client.get("/probe").status_code for _ in range(ANON_LIMIT * 3)]
    assert 429 not in codes
    # Serving unlimited traffic silently is the danger of fail-open, so say so.
    assert client.get("/probe").headers["ratelimit-policy"] == "degraded"


def test_fail_closed_returns_503_not_429(client, limiter, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_fail_open", False)
    limiter._script = raises_outage()

    response = client.get("/probe")
    # 503, because the caller did nothing wrong -- the limiter did.
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


def test_outage_logging_is_throttled(limiter, caplog):
    """One log line per request during an outage would bury the incident."""
    limiter._script = raises_outage()

    async def run():
        await limiter.connect()
        limiter._script = raises_outage()
        for _ in range(50):
            await limiter.check("user", "x", 10)
        await limiter.close()

    with caplog.at_level("ERROR"):
        asyncio.run(run())
    assert len([r for r in caplog.records if "cannot reach Redis" in r.message]) == 1


# ── disabling ────────────────────────────────────────────────────────────────

def test_disabled_limiter_is_a_passthrough(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    codes = [client.get("/probe").status_code for _ in range(ANON_LIMIT * 4)]
    assert 429 not in codes
    assert "ratelimit-limit" not in client.get("/probe").headers


# ── proxy headers ────────────────────────────────────────────────────────────

def test_forwarded_for_is_ignored_by_default(client):
    """Otherwise a fresh XFF per request is a free bypass."""
    codes = [
        client.get("/probe", headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code
        for i in range(ANON_LIMIT + 2)
    ]
    assert 429 in codes


def test_forwarded_for_uses_the_rightmost_hop_when_trusted(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_trust_proxy", True)

    distinct = [
        client.get("/probe", headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code
        for i in range(ANON_LIMIT + 2)
    ]
    assert 429 not in distinct

    # A client can prepend fabricated hops on the left; the right end is what the
    # nearest proxy appended, so that is the entry that must decide the bucket.
    same = [
        client.get("/probe", headers={"X-Forwarded-For": f"1.1.1.{i}, 9.9.9.250"}).status_code
        for i in range(ANON_LIMIT + 2)
    ]
    assert 429 in same
