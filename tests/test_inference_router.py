"""The round-robin inference router: rotation, transparency, failover.

The router's contract has two halves. Requests alternate between replicas; and
the caller cannot tell the router is there -- the same bytes, status codes and
streams come back as if INFERENCE_BASE_URL pointed at a single replica. Both are
pinned here without a real server, with httpx's MockTransport standing in for
the replicas.

What these do not measure is whether two replicas are *faster*. That is a
property of the hardware, not the code, and it is measured in
docs/INFERENCE_REPLICAS.md.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from inference_router import RoundRobin, create_app

A = "http://replica-a:11434"
B = "http://replica-b:11435"
CHAT = "/v1/chat/completions"


def unread(status: int, body: dict) -> httpx.Response:
    """A response whose body has not been read yet, as one off the network is.

    `httpx.Response(json=...)` reads its own body at construction, so the
    router's raw relay would find it already consumed -- a failure no real
    replica can produce.
    """
    return httpx.Response(status, headers={"content-type": "application/json"},
                          stream=httpx.ByteStream(json.dumps(body).encode()))


class Replicas:
    """Fake upstreams. `behaviour` maps an origin to an exception to raise or a
    function returning a response; anything unlisted answers 200 with its name."""

    def __init__(self, **behaviour) -> None:
        self.behaviour = {self._key(k): v for k, v in behaviour.items()}
        self.seen: list[httpx.Request] = []
        self.attempts: list[str] = []

    @staticmethod
    def _key(name: str) -> str:
        return {"a": A, "b": B}[name]

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            origin = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
            self.attempts.append(origin)
            action = self.behaviour.get(origin)
            if isinstance(action, Exception):
                raise type(action)(str(action), request=request)
            self.seen.append(request)
            if callable(action):
                return action(request)
            return unread(200, {"served_by": origin})
        return httpx.MockTransport(handle)


def router(replicas: Replicas, *urls: str) -> TestClient:
    return TestClient(create_app(urls or (A, B), transport=replicas.transport()))


# ─────────────────────────────────────────────────────────────────────────────
# 1. rotation
# ─────────────────────────────────────────────────────────────────────────────

def test_requests_alternate_between_replicas_in_order():
    replicas = Replicas()
    with router(replicas) as client:
        served = [client.post(CHAT, json={"messages": []}).headers["x-inference-upstream"]
                  for _ in range(5)]

    assert served == [A, B, A, B, A]
    assert [str(r.url).split("/v1")[0] for r in replicas.seen] == served


def test_rotation_advances_once_per_request_not_once_per_attempt():
    """A dead replica must not make its neighbour take two turns in a row."""
    rr = RoundRobin([A, B])
    orders = [[u.url for u in rr.order()] for _ in range(3)]
    assert orders == [[A, B], [B, A], [A, B]]


def test_a_single_upstream_is_a_valid_router():
    """The one-replica baseline in the benchmark runs through the same code."""
    replicas = Replicas()
    with router(replicas, A) as client:
        served = {client.get("/v1/models").headers["x-inference-upstream"] for _ in range(3)}
    assert served == {A}


# ─────────────────────────────────────────────────────────────────────────────
# 2. transparency
# ─────────────────────────────────────────────────────────────────────────────

def test_the_request_reaches_the_replica_unchanged():
    replicas = Replicas()
    body = b'{"model":"qwen2.5:1.5b","messages":[{"role":"user","content":"hi"}]}'
    with router(replicas) as client:
        client.post(f"{CHAT}?trace=1", content=body, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer upstream-key",
            "X-Internal-Token": "loopback",
            "Keep-Alive": "timeout=5",
        })

    sent = replicas.seen[0]
    assert sent.method == "POST"
    assert sent.url.path == CHAT              # /v1 is the caller's, kept as-is
    assert sent.url.query == b"trace=1"
    assert sent.content == body
    assert sent.headers["authorization"] == "Bearer upstream-key"
    assert sent.headers["x-internal-token"] == "loopback"
    assert sent.headers["host"] == "replica-a:11434"
    assert "keep-alive" not in sent.headers   # describes the caller's connection


def test_ollama_native_paths_pass_through_as_well_as_v1():
    replicas = Replicas()
    with router(replicas) as client:
        assert client.get("/api/tags").status_code == 200
    assert replicas.seen[0].url.path == "/api/tags"
    assert replicas.seen[0].method == "GET"


def test_a_stream_comes_back_byte_for_byte():
    frames = [b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
              b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
              b"data: [DONE]\n\n"]

    async def sse():
        for frame in frames:
            yield frame

    replicas = Replicas(a=lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=sse()))

    with router(replicas) as client:
        with client.stream("POST", CHAT, json={"stream": True}) as response:
            received = b"".join(response.iter_bytes())
            content_type = response.headers["content-type"]

    assert received == b"".join(frames)
    assert content_type == "text/event-stream"


@pytest.mark.parametrize("status", [400, 404, 500])
def test_a_replicas_error_status_passes_through_and_is_not_retried(status):
    """A request that reached a replica may already have generated. Resending
    it elsewhere could run it twice, so the caller gets the replica's answer."""
    replicas = Replicas(a=lambda request: unread(status, {"error": "model 'nope' not found"}))

    with router(replicas) as client:
        response = client.post(CHAT, json={"model": "nope"})

    assert response.status_code == status
    assert response.json() == {"error": "model 'nope' not found"}
    assert replicas.attempts == [A]


# ─────────────────────────────────────────────────────────────────────────────
# 3. failure
# ─────────────────────────────────────────────────────────────────────────────

def test_a_refused_connection_fails_over_to_the_next_replica():
    replicas = Replicas(a=httpx.ConnectError("connection refused"))
    with router(replicas) as client:
        served = [client.post(CHAT, json={}).headers["x-inference-upstream"] for _ in range(4)]
        stats = client.get("/router/stats").json()

    assert served == [B, B, B, B]
    by_url = {u["url"]: u for u in stats["upstreams"]}
    assert by_url[A]["connect_failures"] == 2     # its two turns, each failed over
    assert by_url[A]["requests"] == 0
    assert by_url[B]["requests"] == 4


def test_no_reachable_replica_is_a_503_that_names_them():
    replicas = Replicas(a=httpx.ConnectError("refused"), b=httpx.ConnectTimeout("slow"))
    with router(replicas) as client:
        response = client.post(CHAT, json={})

    assert response.status_code == 503
    assert response.json()["detail"] == "No inference upstream is reachable."
    assert response.json()["upstreams"] == [A, B]


def test_a_timeout_after_the_request_was_sent_is_a_504_not_a_retry():
    replicas = Replicas(a=httpx.ReadTimeout("no response"))
    with router(replicas) as client:
        response = client.post(CHAT, json={})

    assert response.status_code == 504
    assert replicas.attempts == [A]


# ─────────────────────────────────────────────────────────────────────────────
# 4. accounting
# ─────────────────────────────────────────────────────────────────────────────

def test_stats_count_requests_and_release_every_slot():
    replicas = Replicas()
    with router(replicas) as client:
        for _ in range(3):
            client.post(CHAT, json={})
        stats = client.get("/router/stats").json()

    assert stats["policy"] == "round-robin"
    assert [(u["requests"], u["in_flight"], u["peak_in_flight"]) for u in stats["upstreams"]] \
        == [(2, 0, 1), (1, 0, 1)]
    assert replicas.attempts == [A, B, A]      # the stats route itself is not forwarded


def test_busy_while_idle_counts_a_generation_queued_behind_another_needlessly():
    """The cost of round-robin being blind to load, counted rather than guessed."""
    rr = RoundRobin([A, B, "http://replica-c:11436"])
    a, b, c = rr.upstreams

    rr.order(); rr.acquire(a, generating=True)          # a long answer starts on a
    rr.order(); rr.acquire(b, generating=True); rr.release(b)
    rr.order(); rr.acquire(c, generating=True); rr.release(c)
    assert rr.busy_while_idle == 0

    rr.order(); rr.acquire(a, generating=False)         # a model listing: never queues
    assert rr.busy_while_idle == 0
    rr.release(a)

    rr.order(); rr.acquire(a, generating=True)          # a's turn again, b and c idle
    assert rr.busy_while_idle == 1


@pytest.mark.parametrize("urls", [[], ["127.0.0.1:11434"], ["ftp://replica"]])
def test_the_upstream_list_is_validated(urls):
    with pytest.raises(ValueError):
        RoundRobin(urls)


def test_a_trailing_slash_on_an_upstream_does_not_double_the_path():
    replicas = Replicas()
    with TestClient(create_app([A + "/"], transport=replicas.transport())) as client:
        client.get("/v1/models")
    assert str(replicas.seen[0].url) == f"{A}/v1/models"
