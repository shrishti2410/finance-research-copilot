"""FastAPI proxy in front of an OpenAI-compatible inference server.

The upstream is whatever speaks `/v1/chat/completions`:

    prod / GPU   vLLM              (`vllm serve Qwen/Qwen2.5-7B-Instruct`)
    dev / CPU    Ollama or llama.cpp (`ollama serve`, `llama-server`)

Only INFERENCE_BASE_URL changes between them -- see docs/INFERENCE.md.

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

import json
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

_client: httpx.AsyncClient | None = None


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


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def get_client() -> httpx.AsyncClient:
    if _client is None:
        raise HTTPException(status_code=503, detail="Inference client not initialized.")
    return _client


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

    payload.setdefault("model", settings.inference_model)
    stream = bool(payload.get("stream", False))
    client = get_client()

    # ── Buffered path ────────────────────────────────────────────────────────
    if not stream:
        try:
            resp = await client.post("/chat/completions", json=payload)
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"Inference server unreachable: {exc}") from exc
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    # ── Streaming path ───────────────────────────────────────────────────────
    # send(stream=True) instead of the `stream()` context manager: it lets us
    # inspect the status code BEFORE constructing the StreamingResponse. Once a
    # StreamingResponse starts, headers are already on the wire and a 500 from
    # upstream can no longer be reported as anything but a truncated 200.
    req = client.build_request("POST", "/chat/completions", json=payload)
    try:
        upstream = await client.send(req, stream=True)
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
        },
    )
