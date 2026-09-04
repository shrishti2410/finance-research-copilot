"""Redis-backed per-user rate limiting, as pure ASGI middleware.

Two decisions shape this file.

**Pure ASGI, not BaseHTTPMiddleware.** Starlette's BaseHTTPMiddleware wraps each
response in an anyio task group and pumps it through a memory stream. This app
exists to relay SSE from an inference server byte-for-byte with the lowest
possible TTFT (see api/inference_proxy.py, which refuses to even parse the
frames). Putting a buffering wrapper around that stream would undo the one thing
that path is careful about. A raw ASGI callable adds a dict lookup and a header
mutation, and touches the response body not at all.

**Sliding window log in a Lua script, not INCR.** The naive `INCR` + `EXPIRE`
fixed window lets a caller send `limit` requests in the last instant of one
window and `limit` more in the first instant of the next -- a 2x burst straddling
the boundary, which is exactly the traffic shape a limiter is supposed to stop.
A sorted set of request timestamps has no boundary to straddle. It has to be one
Lua script because `ZCARD`-then-`ZADD` from the client is a check-then-act race:
under concurrency, N requests can all read a count below the limit and all admit
themselves. Redis runs a script atomically, so the count and the insert cannot be
interleaved.
"""

from __future__ import annotations

import logging
import math
import time
import uuid

import redis.asyncio as aioredis
from redis.exceptions import RedisError
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from auth.security import decode_access_token
from core.config import settings

log = logging.getLogger(__name__)

# Health and docs endpoints are exempt. An orchestrator polling /health every two
# seconds would otherwise spend the shared anonymous budget for its IP -- and the
# failure mode is perverse: liveness checks start returning 429, the orchestrator
# calls the process dead, and restarts a container that was perfectly healthy.
EXEMPT_PATHS = frozenset({"/health", "/health/db", "/health/redis", "/openapi.json", "/favicon.ico"})
EXEMPT_PREFIXES = ("/docs", "/redoc")

KEY_PREFIX = "rl:v1"

# KEYS[1] = bucket key
# ARGV[1] = now (ms), ARGV[2] = window (ms), ARGV[3] = limit, ARGV[4] = member
# Returns {allowed, remaining, reset_ms}
SLIDING_WINDOW_LUA = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])

-- Drop everything that has aged out of the window.
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

local count = redis.call('ZCARD', key)
local allowed = 0
if count < limit then
  redis.call('ZADD', key, now, ARGV[4])
  count = count + 1
  allowed = 1
end

-- Always refresh the TTL: an idle key must expire on its own, or a limiter
-- accumulates one key per caller forever.
redis.call('PEXPIRE', key, window)

-- Reset is when the oldest surviving request falls out of the window.
local reset_ms = window
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
if oldest[2] then
  reset_ms = (tonumber(oldest[2]) + window) - now
end
if reset_ms < 0 then reset_ms = 0 end

return {allowed, limit - count, reset_ms}
"""


class Decision:
    __slots__ = ("allowed", "limit", "remaining", "reset_seconds", "degraded")

    def __init__(
        self, allowed: bool, limit: int, remaining: int, reset_seconds: int, degraded: bool = False
    ) -> None:
        self.allowed = allowed
        self.limit = limit
        self.remaining = max(0, remaining)
        self.reset_seconds = reset_seconds
        # True when Redis could not be reached and the request was let through
        # under the fail-open policy. Surfaced on /health/redis so that
        # "limiting is silently off" is an observable state, not a surprise.
        self.degraded = degraded


class RateLimiter:
    """Owns the Redis connection and the sliding-window script."""

    def __init__(self, client: aioredis.Redis | None = None) -> None:
        self._client = client
        self._script = None
        self._last_error_log = 0.0
        self.degraded = False

    async def connect(self) -> None:
        if self._client is None:
            self._client = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=False,
                socket_connect_timeout=2,
                # A limiter must never be the slowest thing in the request. If
                # Redis cannot answer in 250ms, take the fail-open path instead
                # of making every caller wait on it.
                socket_timeout=0.25,
                health_check_interval=30,
            )
        # register_script handles EVALSHA and transparently falls back to EVAL on
        # NOSCRIPT, which is what happens after a Redis restart flushes the cache.
        self._script = self._client.register_script(SLIDING_WINDOW_LUA)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.ping()
        except (RedisError, OSError):
            return False
        return True

    def _log_outage(self, exc: Exception) -> None:
        """Log Redis failures at most twice a minute.

        A Redis outage produces one failure per request; at load that is a log
        line per request, which buries the incident it is reporting.
        """
        now = time.monotonic()
        if now - self._last_error_log > 30:
            self._last_error_log = now
            log.error(
                "Rate limiter cannot reach Redis (%s: %s). Serving %s.",
                type(exc).__name__,
                exc,
                "requests unlimited" if settings.rate_limit_fail_open else "503",
            )

    async def check(self, bucket: str, identity: str, limit: int) -> Decision:
        window_ms = settings.rate_limit_window_seconds * 1000
        key = f"{KEY_PREFIX}:{bucket}:{identity}"
        now_ms = int(time.time() * 1000)
        # Unique member per request: two requests in the same millisecond would
        # otherwise collide on score+value and ZADD would overwrite, quietly
        # under-counting the caller.
        member = f"{now_ms}-{uuid.uuid4().hex[:12]}"

        try:
            allowed, remaining, reset_ms = await self._script(
                keys=[key], args=[now_ms, window_ms, limit, member]
            )
        except (RedisError, OSError) as exc:
            self._log_outage(exc)
            self.degraded = True
            return Decision(
                allowed=settings.rate_limit_fail_open,
                limit=limit,
                remaining=0,
                reset_seconds=settings.rate_limit_window_seconds,
                degraded=True,
            )

        self.degraded = False
        return Decision(
            allowed=bool(allowed),
            limit=limit,
            remaining=int(remaining),
            reset_seconds=max(1, math.ceil(int(reset_ms) / 1000)),
        )


def _client_ip(scope: Scope) -> str:
    """Best available caller address.

    X-Forwarded-For is only consulted when explicitly enabled, because it is a
    request header: anyone can send one. Trusting it without a proxy in front
    that overwrites it hands every caller a free rate-limit bypass -- send a
    random XFF per request and every request lands in its own bucket.

    When it is trusted, the *rightmost* entry is used, not the leftmost. A client
    can prepend fabricated hops to the left; the right end is what the nearest
    proxy appended, so it is the entry closest to something we control.
    """
    if settings.rate_limit_trust_proxy:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                hops = [h.strip() for h in value.decode("latin-1").split(",") if h.strip()]
                if hops:
                    return hops[-1]
    client = scope.get("client")
    return client[0] if client else "unknown"


def _identify(scope: Scope) -> tuple[str, str]:
    """Return (identity, kind) for this request.

    The JWT is decoded here rather than reusing the `get_current_user`
    dependency: middleware runs before routing, so FastAPI's dependency
    injection is not available yet. That is a feature -- identifying the caller
    costs one HMAC verification and no database round-trip, which is what you
    want in front of a limiter whose whole job is to run before expensive work.

    The signature check still means the identity cannot be forged. A revoked or
    deactivated account may still be recognized here for a few minutes, which is
    fine: this decides which bucket to count in, not whether to authorize.
    """
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            parts = value.decode("latin-1").split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                claims = decode_access_token(parts[1])
                if claims:
                    return claims["sub"], "user"
            break
    return _client_ip(scope), "ip"


def _bucket_and_limit(path: str, kind: str) -> tuple[str, int]:
    """Which counter this request belongs to, and its ceiling."""
    if path.startswith("/auth/"):
        # Always IP-keyed and always its own bucket: a login attempt has no user
        # id yet, and credential stuffing must not be able to spend a different
        # budget than the one guarding it.
        return "auth", settings.rate_limit_auth_per_minute
    if kind == "user":
        return "user", settings.rate_limit_per_minute
    return "anon", settings.rate_limit_anon_per_minute


def _headers(decision: Decision) -> dict[str, str]:
    # Names follow the IETF draft (draft-ietf-httpapi-ratelimit-headers). The
    # X- prefixed spellings are still more widely recognized by clients, so both
    # are sent; they cost a few bytes and save an integration argument.
    values = {
        "RateLimit-Limit": str(decision.limit),
        "RateLimit-Remaining": str(decision.remaining),
        "RateLimit-Reset": str(decision.reset_seconds),
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_seconds),
    }
    if decision.degraded:
        values["RateLimit-Policy"] = "degraded"
    return values


class RateLimitMiddleware:
    """Counts every HTTP request against its caller's bucket before routing."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan and websocket scopes pass straight through: the limiter counts
        # HTTP requests, and swallowing a lifespan message would hang startup.
        if scope["type"] != "http" or not settings.rate_limit_enabled:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
            return await self.app(scope, receive, send)

        identity, kind = _identify(scope)
        bucket, limit = _bucket_and_limit(path, kind)
        decision = await self.limiter.check(bucket, identity, limit)

        if not decision.allowed:
            if decision.degraded:
                # fail-closed: the limiter could not decide, so it refuses rather
                # than guesses. Distinct status from a genuine 429.
                response = JSONResponse(
                    status_code=503,
                    content={"detail": "Rate limiter unavailable."},
                    headers={"Retry-After": "5"},
                )
            else:
                response = JSONResponse(
                    status_code=429,
                    content={
                        "detail": (
                            f"Rate limit exceeded: {decision.limit} requests per "
                            f"{settings.rate_limit_window_seconds}s."
                        ),
                        "retry_after": decision.reset_seconds,
                    },
                    # Retry-After is what a well-behaved client actually reads to
                    # back off. Without it the usual response to a 429 is an
                    # immediate retry, which is how a limiter becomes a hot loop.
                    headers={"Retry-After": str(decision.reset_seconds), **_headers(decision)},
                )
            return await response(scope, receive, send)

        extra = _headers(decision)

        async def send_with_headers(message: Message) -> None:
            # Headers are injected into the response-start message rather than by
            # wrapping the response object, so a streaming body is never touched.
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).update(extra)
            await send(message)

        await self.app(scope, receive, send_with_headers)
