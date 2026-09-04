# Rate limiting

Redis-backed sliding window, enforced by ASGI middleware
([`api/rate_limit.py`](../api/rate_limit.py)) before routing, authentication, or
any database work happens on a caller's behalf.

```
request ─▶ RateLimitMiddleware ─▶ router ─▶ auth ─▶ handler
              │
              └─▶ Redis (one Lua script, one round trip)
```

## Limits

| bucket | key | default | why |
|---|---|---|---|
| `user` | user id from the JWT | 60/min | follows a person across devices and IPs |
| `anon` | client IP | 20/min | an IP is a weak identity; shared NATs share it |
| `auth` | client IP, on `/auth/*` | 10/min | login and signup are the brute-force surface |

All three are `RATE_LIMIT_*` settings; the window is `RATE_LIMIT_WINDOW_SECONDS`
(60 by default, so "per minute" is a default and not an assumption baked into
the code).

`/auth/*` is always IP-keyed **even when a valid token is present**. A login
attempt has no user id yet, so credential stuffing would otherwise be counted
against whatever bucket the attacker chose — including none.

Exempt: `/health`, `/health/db`, `/health/redis`, `/openapi.json`, `/docs`,
`/redoc`. An orchestrator polling liveness every two seconds would otherwise
spend the shared anonymous budget for its IP, and the failure mode is perverse:
health checks start returning 429, the orchestrator concludes the process is
dead, and restarts a container that was fine.

## Why a sliding window, and why Lua

The obvious implementation is `INCR` on a key like `rl:user:123:<minute>` with an
`EXPIRE`. It is one command and it is wrong at the boundary: a caller can send
`limit` requests in the last instant of one minute and `limit` more in the first
instant of the next — a 2× burst, which is the exact traffic shape a limiter
exists to stop.

A sorted set of request timestamps has no boundary to straddle. Each request
drops entries older than the window, counts what is left, and adds itself if
there is room.

It has to be **one Lua script** because `ZCARD`-then-`ZADD` from the client is a
check-then-act race. Under concurrency, N requests can each read a count below
the limit and each admit themselves — the limiter fails open precisely when it
is under the load it was installed to handle. Redis executes a script
atomically, so the count and the insert cannot interleave.

Cost: one round trip per request, and up to `limit` sorted-set members per active
caller (60 small entries at the default). The key carries a TTL of one window, so
idle callers cost nothing — without that `PEXPIRE`, a limiter accumulates one key
per caller forever.

The script is loaded with `register_script`, which uses `EVALSHA` and falls back
to `EVAL` on `NOSCRIPT` — the case that happens after a Redis restart flushes the
script cache.

## Identity

The JWT is decoded in the middleware rather than through the `get_current_user`
dependency, because middleware runs before routing and dependency injection is
not available yet. That is a feature: identifying the caller costs one HMAC
verification and **no database round trip**, which is what you want in front of a
limiter whose entire job is to run before expensive work.

The signature is still checked, so identity cannot be forged. A deactivated
account may keep its own bucket for a few minutes after being disabled — which is
fine, because this decides *which counter to use*, not whether to authorize.

`X-Forwarded-For` is ignored unless `RATE_LIMIT_TRUST_PROXY=true`. It is a
request header; anyone can send one. Trusting it without a proxy in front that
overwrites it hands every caller a free bypass — send a random XFF per request
and every request lands in its own bucket. When it *is* trusted, the **rightmost**
entry is used: a client can prepend fabricated hops on the left, so the right end
— what the nearest proxy appended — is the entry closest to something we control.

## When Redis is down

`RATE_LIMIT_FAIL_OPEN` decides, and it is a genuine trade-off with no free answer:

- **`true` (default)** — requests are served unlimited. A Redis outage does not
  become an API outage. The risk is that enforcement silently stops.
- **`false`** — requests get `503`. Limits hold, but a Redis hiccup takes the
  whole API down with it.

Either way the failure is made visible rather than swallowed:

- `GET /health/redis` reports `"limiting": false` when Redis is unreachable. An
  outage nobody can see is worse than one that pages.
- Degraded responses carry `RateLimit-Policy: degraded`.
- The outage is logged at most twice a minute. One line per request would
  produce a log entry per request at load, burying the incident it reports.

Note that `503` under fail-closed is deliberately **not** `429`: the caller did
nothing wrong: the limiter did.

## Response headers

Every limited response carries both the IETF draft names
(`RateLimit-Limit`, `-Remaining`, `-Reset`) and the `X-` prefixed spellings that
more clients actually recognize. `429` responses add `Retry-After` — without it,
the usual client reaction to a 429 is an immediate retry, which turns the limiter
into a hot loop.

```
HTTP/1.1 429 Too Many Requests
Retry-After: 42
RateLimit-Limit: 60
RateLimit-Remaining: 0
RateLimit-Reset: 42

{"detail": "Rate limit exceeded: 60 requests per 60s.", "retry_after": 42}
```

## Why pure ASGI and not `BaseHTTPMiddleware`

Starlette's `BaseHTTPMiddleware` wraps each response in an anyio task group and
pumps the body through a memory stream. This app exists to relay SSE from an
inference server byte-for-byte at the lowest possible TTFT — see
[`api/inference_proxy.py`](../api/inference_proxy.py), which refuses to even
parse the frames for that reason. Wrapping that stream would undo the one thing
that path is careful about.

A raw ASGI callable adds a dict lookup and a header mutation on the
`http.response.start` message, and never touches the body. Measured through a
real uvicorn against a live model: 95 SSE chunks, first at 2.28 s, last at
6.59 s — still streaming, `X-Accel-Buffering: no` intact, no `Content-Length`.

## Running and testing

```bash
docker compose -f docker-compose.dev.yml up -d      # Postgres + Redis
pytest tests/test_rate_limit.py
```

The tests use `fakeredis[lua]`, which really executes the sliding-window script,
so they need neither a Redis server nor a database.

What they cannot cover: Redis's own atomicity under genuine parallelism.
fakeredis runs in-process, so the concurrency test proves the script's logic
never double-admits — not that a real Redis serializes it. That second part is a
property of Redis itself (single-threaded command execution), and is the whole
reason the logic lives in a script.

Redis runs with `--save "" --appendonly no`: rate-limit counters are worth less
than the window they cover, so losing them on restart costs one window of
enforcement, while fsyncing them would put a disk write in front of every
request. `maxmemory-policy allkeys-lru` is the safety net — if the keyspace ever
grows unexpectedly, evict old counters rather than start refusing writes.

## Known limits

- **Per-process, not per-deployment, for anonymous callers behind one NAT.**
  Everyone sharing an office IP shares the 20/min anonymous budget. Authenticated
  callers do not have this problem, which is one more reason to require a token.
- **No per-endpoint costs.** A `/conversations` list and a full RAG completion
  each cost one unit, though they are nothing alike in expense. Weighted costs
  (or a separate token-based budget for inference) are the natural next step once
  the agent exists.
- **No burst allowance.** A sliding window admits a steady rate; it has no notion
  of saved-up credit for a client that was idle. A token bucket would, at the
  cost of a second Redis value per caller.
