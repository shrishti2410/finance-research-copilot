"""Round-robin router in front of several identical inference servers.

    python -m inference_router --port 11400 \\
        --upstream http://127.0.0.1:11434 --upstream http://127.0.0.1:11435

    INFERENCE_BASE_URL=http://127.0.0.1:11400/v1      # the app's only change

Not to be confused with `agent_router_model`, which routes the *steps* of one
agent run between a small and a large model. This routes whole *requests*
between replicas of the same server, and knows nothing about models.

What this is: data parallelism. Every replica holds a complete copy of every
model it serves, and each request runs start to finish on exactly one of them.
That raises how many requests can be served at once; it does nothing for how
fast any single request runs. Splitting one model across several GPUs (tensor or
pipeline parallelism) is the other kind of distributed inference, it needs
hardware this project does not have, and no router can stand in for it. See
docs/INFERENCE_REPLICAS.md, which also records what two replicas measurably buy
on a CPU host.

Why a separate process rather than a list of URLs in api/inference_proxy.py:
it is the shape a real deployment has -- a load balancer in front of inference
nodes -- and the application does not change at all. It speaks HTTP and nothing
else, so /v1/* and Ollama's native /api/* pass through alike, and it follows the
proxy's one hard rule: bytes are relayed untouched and a stream is never
buffered.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Iterable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

# Headers that describe one connection rather than the message, so they must not
# be copied from one leg of the proxy to the other (RFC 9110 section 7.6.1).
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
})
# Host names the router, not the upstream; content-length is recomputed by httpx
# from the body it actually sends.
REQUEST_DROP = HOP_BY_HOP | {"host", "content-length"}

UPSTREAM_HEADER = "x-inference-upstream"


@dataclass
class Upstream:
    url: str
    requests: int = 0
    in_flight: int = 0
    peak_in_flight: int = 0
    connect_failures: int = 0


class RoundRobin:
    """Hands out upstreams in strict rotation, blind to how busy each one is.

    No lock: the app runs on one event loop, and nothing here awaits between
    reading the counters and changing them.
    """

    def __init__(self, urls: Iterable[str]) -> None:
        self.upstreams = [Upstream(_normalise(u)) for u in urls]
        if not self.upstreams:
            raise ValueError("the router needs at least one upstream")
        self._next = 0
        # Generation requests sent to a replica that already had one running
        # while another replica sat idle. With OLLAMA_NUM_PARALLEL=1 each of
        # these waited in a queue that did not need to exist -- the measurable
        # cost of round-robin's blindness, and the number that says whether a
        # least-busy policy would be worth writing.
        self.busy_while_idle = 0

    def order(self) -> list[Upstream]:
        """This request's rotation: its turn first, then the others as fallbacks.

        The pointer advances once per request, not once per attempt, so a dead
        replica does not make its neighbour take two turns in a row.
        """
        n = len(self.upstreams)
        start = self._next
        self._next = (start + 1) % n
        return [self.upstreams[(start + i) % n] for i in range(n)]

    def acquire(self, upstream: Upstream, generating: bool) -> None:
        # GETs are model listings and health probes, which Ollama answers
        # without waiting for a generation slot, so they cannot queue.
        if generating and upstream.in_flight and any(
            other.in_flight == 0 for other in self.upstreams if other is not upstream
        ):
            self.busy_while_idle += 1
        upstream.in_flight += 1
        upstream.peak_in_flight = max(upstream.peak_in_flight, upstream.in_flight)

    def release(self, upstream: Upstream) -> None:
        upstream.in_flight -= 1

    def stats(self) -> dict:
        return {
            "policy": "round-robin",
            "upstreams": [vars(u).copy() for u in self.upstreams],
            "busy_while_idle": self.busy_while_idle,
        }


def _normalise(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"upstream must be an http(s) URL, got {url!r}")
    return url


def create_app(
    upstream_urls: Iterable[str],
    *,
    connect_timeout: float = 5.0,
    read_timeout: float = 900.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the router. `transport` exists so tests can stand in for upstreams.

    read_timeout is deliberately longer than the app's INFERENCE_READ_TIMEOUT
    (300s): the caller owns the policy on how long to wait, and a router that
    timed out first would turn the caller's clean inference_error into a
    confusing 504 from a hop it does not know exists.
    """
    rr = RoundRobin(upstream_urls)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout,
                                  write=30.0, pool=None),
            # Unbounded. A router that queued requests in its own connection
            # pool would hide where the wait really is; queueing belongs to the
            # replicas, where the load test can see it.
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=32),
        )
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(title="inference-router", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.router = rr

    @app.get("/router/stats")
    async def stats() -> dict:
        return rr.stats()

    @app.api_route("/{path:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def forward(path: str, request: Request) -> Response:
        client: httpx.AsyncClient = request.app.state.client
        body = await request.body()
        headers = [(k, v) for k, v in request.headers.items() if k not in REQUEST_DROP]
        query = f"?{request.url.query}" if request.url.query else ""
        generating = request.method != "GET"

        for upstream in rr.order():
            outgoing = client.build_request(
                request.method, f"{upstream.url}/{path}{query}",
                headers=headers, content=body or None,
            )
            rr.acquire(upstream, generating)
            try:
                response = await client.send(outgoing, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # The request never reached this replica, so trying the next one
                # cannot run a generation twice. Nothing else is retried: once a
                # request is on the wire, resending it could.
                rr.release(upstream)
                upstream.connect_failures += 1
                continue
            except httpx.TimeoutException as exc:
                rr.release(upstream)
                return _error(504, f"Upstream {upstream.url} timed out: {type(exc).__name__}")
            except httpx.RequestError as exc:
                rr.release(upstream)
                return _error(502, f"Upstream {upstream.url} failed: {type(exc).__name__}")

            upstream.requests += 1
            return StreamingResponse(
                _relay(response, lambda u=upstream: rr.release(u)),
                status_code=response.status_code,
                headers={
                    **{k: v for k, v in response.headers.items() if k not in HOP_BY_HOP},
                    UPSTREAM_HEADER: upstream.url,
                },
            )

        return JSONResponse(
            status_code=503,
            content={
                "detail": "No inference upstream is reachable.",
                "upstreams": [u.url for u in rr.upstreams],
            },
        )

    return app


async def _relay(response: httpx.Response, release) -> AsyncIterator[bytes]:
    """Relay raw bytes, then close the upstream and free the replica's slot.

    aiter_raw rather than aiter_bytes: the bytes go out exactly as they came in,
    compressed or not, so the copied content-length and content-encoding stay
    true. The `finally` also runs when the client disconnects mid-stream, which
    closes the upstream connection and lets the replica stop generating.
    """
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await response.aclose()
        release()


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})
