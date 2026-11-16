"""FastAPI proxy in front of an OpenAI-compatible inference server.

The upstream is whatever speaks `/v1/chat/completions`:

    prod / GPU   vLLM              (`vllm serve Qwen/Qwen2.5-7B-Instruct`)
    dev / CPU    Ollama or llama.cpp (`ollama serve`, `llama-server`)

Only INFERENCE_BASE_URL changes between them -- see docs/INFERENCE.md.

A hosted provider can sit beside the local one. The agent names the provider it
wants for a call in the X-Inference-Provider header, and this proxy holds that
provider's key, so the key never leaves the server and never reaches the agent
or a browser. The header is honoured only on the app's own internal calls: the
/v1 routes are reachable without a login, and a hosted provider is metered, so
an outside caller asking for it is refused rather than served on the operator's
key. Everyone else gets the local default, exactly as before.

Why proxy at all instead of letting clients hit vLLM directly:
  - vLLM has no real auth; this is where the app's own auth/rate limiting goes
  - one place to pin defaults (model, max_tokens) and log token usage
  - the browser only ever sees our origin, so CORS and keys stay server-side
  - later, `agent/` can be spliced in without the client noticing

Streaming is forwarded byte-for-byte as Server-Sent Events. We do not parse,
buffer, or re-serialize the SSE frames -- reassembling them would add latency to
every token and is the one thing a proxy on this path must not do.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.config import settings

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

# Config comes from core.config.Settings, not bare os.getenv. That matters:
# Settings loads `.env`, os.getenv does not. Reading the environment directly
# here meant INFERENCE_BASE_URL set in .env -- exactly as .env.example documents
# -- was silently ignored, and the proxy fell back to the vLLM default port
# while the rest of the app (database, auth, Redis) honoured the same file.
#
# Values are read at use time rather than snapshotted into module constants, so
# a test can monkeypatch settings and so nothing depends on import order.


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.inference_connect_timeout,
        read=settings.inference_read_timeout,
        write=30.0,
        pool=30.0,
    )

router = APIRouter(tags=["inference"])

# The provider a request gets when it names none: whatever INFERENCE_BASE_URL is.
DEFAULT_PROVIDER = "ollama"
PROVIDER_HEADER = "x-inference-provider"

_client: httpx.AsyncClient | None = None
# Hosted providers, by name. Only those with a key configured are present.
_hosted: dict[str, httpx.AsyncClient] = {}


def _hosted_providers() -> dict[str, tuple[str, str]]:
    """name -> (base URL, key) for each hosted provider this build knows.

    Adding a provider is a line here and two settings. Both speak the OpenAI
    chat-completions API with a bearer key, so nothing else differs per
    provider.
    """
    return {
        "groq": (settings.groq_base_url, settings.groq_api_key),
        "cerebras": (settings.cerebras_base_url, settings.cerebras_api_key),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Client lifecycle
#
# One AsyncClient for the whole process. Building one per request would open a
# fresh TCP connection every time and throw away connection reuse -- measurable
# overhead when tokens are streaming.
# ─────────────────────────────────────────────────────────────────────────────

async def startup() -> None:
    global _client
    key = settings.inference_api_key
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    _client = httpx.AsyncClient(
        base_url=settings.inference_base_url, timeout=_timeout(), headers=headers
    )
    for name, (base_url, api_key) in _hosted_providers().items():
        if api_key:
            _hosted[name] = httpx.AsyncClient(
                base_url=base_url, timeout=_timeout(),
                headers={"Authorization": f"Bearer {api_key}"},
            )


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
    for client in _hosted.values():
        await client.aclose()
    _hosted.clear()


def get_client(provider: str = DEFAULT_PROVIDER) -> httpx.AsyncClient:
    if provider == DEFAULT_PROVIDER:
        if _client is None:
            raise HTTPException(status_code=503, detail="Inference client not initialized.")
        return _client
    client = _hosted.get(provider)
    if client is None:
        # Known provider, no key. Names the setting, never the key itself.
        raise HTTPException(
            status_code=503,
            detail=f"Provider {provider!r} is not configured: its API key is not set in .env.",
        )
    return client


# ─────────────────────────────────────────────────────────────────────────────
# A hosted provider's rate limit
#
# core/retry.py keeps retries away from inference on purpose: a stuck local model
# is not a blip, and asking again only waits twice. A hosted 429 is the opposite
# case. The provider is healthy, it says exactly how long to wait, and the
# request will succeed after that -- measured on Groq's free tier, whose limit is
# 8,000 tokens a minute against roughly 5,000 per question, so most questions
# meet it. Failing the turn there would make the provider unusable; waiting the
# stated time makes it merely slower.
#
# The wait is reported to the caller in a response header, so latency can be
# quoted for the model and for the throttle separately. It is bounded: a limit
# that does not clear within the attempts below is returned as the 429 it is.
# ─────────────────────────────────────────────────────────────────────────────

log = logging.getLogger(__name__)

RATE_LIMIT_ATTEMPTS = 8
RATE_LIMIT_MAX_WAIT = 75.0          # one wait; a per-minute limit clears within this
RATE_LIMIT_WAIT_HEADER = "x-rate-limit-wait-ms"

_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?$")


def _retry_after_seconds(headers) -> float:
    """How long the provider asks us to wait, from the headers of a 429."""
    raw = headers.get("retry-after")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    # Groq also states when the token budget refills, as "39.33s" or "1m2.5s".
    # Cerebras documents no rate-limit headers (checked 2026-11-16), so until a
    # real 429 from it has been seen, a Cerebras limit without retry-after
    # falls to the default below and is retried up to RATE_LIMIT_ATTEMPTS times.
    match = _DURATION.match((headers.get("x-ratelimit-reset-tokens") or "").strip())
    if match and (match.group(1) or match.group(2)):
        return float(match.group(1) or 0) * 60 + float(match.group(2) or 0)
    return 5.0


async def _send_waiting_out_rate_limits(client, build, *, stream: bool, hosted: bool):
    """Send a request; on a hosted 429, wait as told and send again.

    Returns (response, seconds spent waiting). `build` makes a fresh request for
    each attempt. The local default is never retried.
    """
    waited = 0.0
    for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
        response = await client.send(build(), stream=stream)
        if not hosted or response.status_code != 429 or attempt == RATE_LIMIT_ATTEMPTS:
            return response, waited
        pause = min(max(_retry_after_seconds(response.headers), 0.5), RATE_LIMIT_MAX_WAIT)
        await response.aclose()
        log.warning("inference proxy: provider rate limit; waiting %.1fs "
                    "[attempt %d of %d]", pause, attempt, RATE_LIMIT_ATTEMPTS)
        await asyncio.sleep(pause)
        waited += pause
    return response, waited  # pragma: no cover - the loop always returns


def _is_internal(request: Request) -> bool:
    """Whether this is the app calling itself, by its per-process token."""
    expected = settings.internal_token
    given = request.headers.get("x-internal-token", "")
    return bool(expected) and hmac.compare_digest(given, expected)


def provider_for(request: Request) -> str:
    """Which provider serves this request.

    The default unless the header names another. A hosted provider is granted
    only to internal calls; see the module docstring for why.
    """
    asked = (request.headers.get(PROVIDER_HEADER) or DEFAULT_PROVIDER).strip().lower()
    if asked == DEFAULT_PROVIDER:
        return DEFAULT_PROVIDER
    if asked not in _hosted_providers():
        raise HTTPException(
            status_code=400,
            detail=f"Unknown inference provider {asked!r}. "
                   f"Known: {DEFAULT_PROVIDER}, {', '.join(_hosted_providers())}.",
        )
    if not _is_internal(request):
        raise HTTPException(
            status_code=403,
            detail="A hosted inference provider is available to the application only.",
        )
    return asked


# ─────────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health")
async def health() -> JSONResponse:
    """Liveness for this proxy, plus a real readiness probe of the upstream.

    Returns 200 only when the inference server actually answers. /v1/models is
    the cheapest endpoint that proves the engine finished loading weights --
    vLLM binds its port before the model is resident, so a TCP check would go
    green while requests still 500.
    """
    started = time.perf_counter()
    try:
        resp = await get_client().get("/models", timeout=httpx.Timeout(connect=2, read=5, write=5, pool=5))
        latency_ms = round((time.perf_counter() - started) * 1000, 1)

        if resp.status_code != 200:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "upstream": {"url": settings.inference_base_url, "reachable": True,
                                 "status_code": resp.status_code, "latency_ms": latency_ms},
                },
            )

        models = [m.get("id") for m in resp.json().get("data", [])]
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "upstream": {"url": settings.inference_base_url, "reachable": True,
                             "latency_ms": latency_ms, "models": models},
                "default_model": settings.inference_model,
            },
        )

    except httpx.RequestError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "upstream": {"url": settings.inference_base_url, "reachable": False,
                             "error": f"{type(exc).__name__}: {exc}"},
                "hint": "Is the inference server running? See docs/INFERENCE.md",
            },
        )


@router.get("/models")
async def list_models() -> Any:
    """Passthrough, so clients can discover what the upstream is actually serving."""
    try:
        resp = await get_client().get("/models")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Inference server unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=resp.json())


# ─────────────────────────────────────────────────────────────────────────────
# Chat completions
# ─────────────────────────────────────────────────────────────────────────────

async def _relay_sse(upstream: httpx.Response) -> AsyncIterator[bytes]:
    """Yield upstream bytes untouched, and always close the upstream response.

    The `finally` is load-bearing. If the browser navigates away mid-stream,
    Starlette cancels this generator; without an explicit close the upstream
    request would linger and hold one of vLLM's scheduler slots hostage.
    """
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
    finally:
        await upstream.aclose()


@router.post("/chat/completions")
async def chat_completions(request: Request) -> Any:
    """Forward a chat request to the inference server, streaming or buffered."""
    try:
        payload: dict[str, Any] = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc

    if not payload.get("messages"):
        raise HTTPException(status_code=400, detail="Field 'messages' is required and must be non-empty.")

    provider = provider_for(request)
    if provider == DEFAULT_PROVIDER:
        payload.setdefault("model", settings.inference_model)
    elif not payload.get("model"):
        # The local default model does not exist on a hosted provider.
        raise HTTPException(status_code=400,
                            detail=f"Field 'model' is required for provider {provider!r}.")
    stream = bool(payload.get("stream", False))
    client = get_client(provider)

    hosted = provider != DEFAULT_PROVIDER

    def build() -> httpx.Request:
        return client.build_request("POST", "/chat/completions", json=payload)

    # ── Buffered path ────────────────────────────────────────────────────────
    if not stream:
        try:
            resp, waited = await _send_waiting_out_rate_limits(
                client, build, stream=False, hosted=hosted)
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"Inference server unreachable: {exc}") from exc
        return JSONResponse(status_code=resp.status_code, content=resp.json(),
                            headers={RATE_LIMIT_WAIT_HEADER: str(round(waited * 1000))})

    # ── Streaming path ───────────────────────────────────────────────────────
    # send(stream=True) instead of the `stream()` context manager: it lets us
    # inspect the status code BEFORE constructing the StreamingResponse. Once a
    # StreamingResponse starts, headers are already on the wire and a 500 from
    # upstream can no longer be reported as anything but a truncated 200.
    try:
        upstream, waited = await _send_waiting_out_rate_limits(
            client, build, stream=True, hosted=hosted)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Inference server unreachable: {exc}") from exc

    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = body.decode("utf-8", errors="replace")
        raise HTTPException(status_code=upstream.status_code, detail=detail)

    return StreamingResponse(
        _relay_sse(upstream),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Tells nginx not to buffer the response. Without it a reverse proxy
            # will happily collect the whole stream and deliver it in one lump,
            # which looks exactly like "streaming is broken".
            "X-Accel-Buffering": "no",
            RATE_LIMIT_WAIT_HEADER: str(round(waited * 1000)),
        },
    )
