"""FastAPI application entrypoint.

Run: `uvicorn api.main:app --reload --port 8000`

Mounted so far:
  /health                 process liveness (no upstream dependency)
  /auth/*                 signup, login, current user
  /conversations/*        threads and their messages
  /ask                    put a question to the agent loop
  /v1/health              readiness, including the inference server
  /v1/models              what the inference server is serving
  /v1/chat/completions    proxied chat, streaming or buffered

`/v1` is reserved for the OpenAI-compatible passthrough, because that is where a
client SDK expects to find it. This app's own API lives at the root.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from api import inference_proxy, routes, routes_auth, routes_chat
from api.rate_limit import RateLimiter, RateLimitMiddleware
from core.config import settings
from db.base import DatabaseUnavailable, SessionLocal, dispose_engine

log = logging.getLogger(__name__)

limiter = RateLimiter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.insecure_jwt_secret:
        # Loud, but not fatal: a dev box should still boot without a .env, while
        # nobody should be able to say they were not told.
        log.warning(
            "JWT_SECRET is the built-in development default. Every token this "
            "process issues is forgeable by anyone with the source. Set JWT_SECRET "
            "before exposing this to a network."
        )
    # Open the shared HTTP client once at boot, close it on shutdown.
    await inference_proxy.startup()
    # Does not connect: redis-py dials lazily, so a Redis that is down at boot
    # degrades per the fail-open policy instead of preventing startup.
    await limiter.connect()
    yield
    await inference_proxy.shutdown()
    await limiter.close()
    await dispose_engine()


app = FastAPI(title="Finance Research Copilot", version="0.0.1", lifespan=lifespan)

# Outermost middleware, so a caller over their limit is rejected before routing,
# authentication, or any database work happens on their behalf.
app.add_middleware(RateLimitMiddleware, limiter=limiter)

app.include_router(routes_auth.router)
app.include_router(routes_chat.router)
app.include_router(routes.router)
app.include_router(inference_proxy.router, prefix="/v1")


@app.exception_handler(DatabaseUnavailable)
async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """A database that is down is a 503, not a 500.

    Without this, "Postgres isn't running" and "this endpoint has a bug" are the
    same opaque 500 to the caller, and the same non-actionable page to whoever is
    on call. Mirrors how the inference proxy reports an unreachable upstream.
    The driver's message is deliberately not echoed -- it carries the DSN.
    """
    log.error("Database unavailable on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Database unavailable.",
            "hint": "Is Postgres running and migrated? See docs/DATABASE.md",
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only. Deliberately does not touch the inference server or the
    database, so a container orchestrator won't restart this process just because
    vLLM is slow to load. Use /health/db and /v1/health for readiness."""
    return {"status": "ok"}


@app.get("/health/db")
async def health_db() -> JSONResponse:
    """Readiness for the database. Separate from /health for the same reason
    /v1/health is: a readiness failure should stop traffic, not restart the pod."""
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 -- any failure here means "not ready"
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "error": f"{type(exc).__name__}"},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})


@app.get("/health/redis")
async def health_redis() -> JSONResponse:
    """Readiness for the rate limiter.

    200 with `"limiting": false` is the important case: under the default
    fail-open policy a Redis outage silently stops enforcing limits, and an
    outage nobody can see is worse than one that pages. This endpoint is what
    makes that state visible.
    """
    reachable = await limiter.ping()
    enforcing = reachable and settings.rate_limit_enabled
    return JSONResponse(
        status_code=200 if reachable else 503,
        content={
            "status": "ok" if reachable else "unavailable",
            "limiting": enforcing,
            "policy": "fail-open" if settings.rate_limit_fail_open else "fail-closed",
            "limits_per_minute": {
                "user": settings.rate_limit_per_minute,
                "anonymous": settings.rate_limit_anon_per_minute,
                "auth_endpoints": settings.rate_limit_auth_per_minute,
            },
        },
    )


# from api.routes import router
# app.include_router(router)
