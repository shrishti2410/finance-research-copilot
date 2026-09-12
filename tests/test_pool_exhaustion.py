"""Saturation must look like saturation, not like a bug.

The load test in `benchmarks/LOAD_TEST_RESULTS.md` found that ten concurrent
questions produced 25 connection-pool timeouts and 33 **unhandled 500s**. The
cause was a one-line gap: `db/base.py` translated `OperationalError`,
`InterfaceError` and `OSError` into `DatabaseUnavailable` -> 503, but a pool
timeout raises `sqlalchemy.exc.TimeoutError`, whose MRO is

    TimeoutError -> SQLAlchemyError -> Exception

so it matched none of them and reached the caller as a 500 after a 30-second
wait. A client cannot tell that apart from a crash, and "500" sends whoever is on
call looking for a bug rather than for capacity.

## How these tests are split, and why

The two layers are tested by different means on purpose.

- **`get_session` is tested against a really exhausted pool.** The bug was about
  *which exception SQLAlchemy raises*, so a test that hand-raises one would have
  passed against the broken code. It has to be real.
- **The API handler is tested by raising that same real exception class** from
  `SessionLocal`. Holding a connection from the test's own event loop while
  `TestClient` runs the app in another cannot work -- asyncpg connections belong
  to the loop that opened them -- and the handler does not care where the
  exception came from, only what it is. The class's realism is established by the
  tests above it rather than assumed.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

import db.base as db_base
from api.main import app
from core.config import settings
from db.base import ConnectionPoolExhausted, DatabaseUnavailable, get_session

# A pool of exactly one, no overflow, almost no patience. The deployment's 15
# would need 15 held connections and a 30-second wait per test; the behaviour
# under test is identical and this runs in milliseconds.
TINY_POOL = {"pool_size": 1, "max_overflow": 0, "pool_timeout": 0.25}

# The message SQLAlchemy actually produces, copied from the load test's failure
# report so the assertions below are matching real text.
REAL_POOL_TIMEOUT_MESSAGE = (
    "QueuePool limit of size 5 overflow 10 reached, connection timed out, "
    "timeout 30.00"
)


def with_tiny_pool(body: Callable[[AsyncEngine], Awaitable[None]]) -> None:
    """Run `body` with `SessionLocal` pointed at a one-connection pool.

    Engine created, used and disposed inside a single event loop. Creating it in a
    fixture and using it under `asyncio.run` fails in a way worth naming: asyncpg
    connections are bound to the loop that opened them, so the pool ends up
    holding handles a later loop cannot write to, and teardown dies with
    `AttributeError: 'NoneType' object has no attribute 'send'` rather than
    anything that mentions event loops.

    `get_session` looks `SessionLocal` up in the module at call time, so patching
    the attribute redirects every route without touching the routes.
    """

    async def runner() -> None:
        engine = create_async_engine(settings.database_url, **TINY_POOL)
        original = db_base.SessionLocal
        db_base.SessionLocal = async_sessionmaker(
            engine, expire_on_commit=False, autoflush=False
        )
        try:
            await body(engine)
        finally:
            db_base.SessionLocal = original
            await engine.dispose()

    asyncio.run(runner())


# ── the class hierarchy, which is where the bug lived ────────────────────────

def test_the_pool_timeout_is_the_exception_nobody_expected():
    """Documents the actual defect: the hierarchy, not the handler.

    If a future SQLAlchemy makes pool timeouts an OperationalError, this fails and
    the dedicated clause in `get_session` becomes removable. That is the only
    thing that would make removing it safe.
    """
    assert not issubclass(PoolTimeout, (OperationalError, InterfaceError, OSError)), (
        "pool timeouts are now covered by the original tuple; the dedicated "
        "clause in db/base.get_session may be redundant"
    )


def test_pool_exhaustion_is_a_kind_of_database_unavailable():
    """Why one exception handler still suffices.

    `api/main.py` registers a handler for `DatabaseUnavailable` only, and
    Starlette resolves handlers along the raised exception's MRO. The subclass
    exists to carry a different message, not to need a second registration.
    """
    assert issubclass(ConnectionPoolExhausted, DatabaseUnavailable)


# ── get_session, against a pool that is really full ─────────────────────────

async def drive_like_fastapi(agen, work) -> None:
    """Run `work(session)` the way a route does, and deliver its failure back.

    Two facts make the obvious version of this test wrong, and both were found by
    writing the obvious version first:

    1. **A session checks out lazily.** `SessionLocal()` touches no connection;
       the pool is only consulted at the first query. So `await agen.__anext__()`
       against a full pool raises nothing at all, and a test that stops there
       passes whether or not the translation exists.
    2. **The route body runs at the `yield`.** FastAPI throws a route's exception
       back into the dependency generator, which is how `get_session`'s `except`
       clauses ever see a query failure. `athrow` is that mechanism exactly, so
       this exercises the real path rather than an approximation of it.
    """
    session = await agen.__anext__()
    try:
        await work(session)
    except BaseException as exc:                     # noqa: BLE001 - re-delivered below
        await agen.athrow(exc)
    else:
        pytest.fail("the pool was not exhausted; the query succeeded")


@pytest.mark.parametrize("attempt", [1, 2])
def test_get_session_translates_a_really_full_pool(attempt):
    """Hold the only connection, then have a "route" try to query.

    Parametrized twice to prove the failure is repeatable rather than a one-off
    that leaves the pool broken: a translation that leaked the checkout would pass
    once and then fail differently.
    """

    async def body(engine: AsyncEngine) -> None:
        async with engine.connect():                 # the pool's single connection
            agen = get_session()
            with pytest.raises(ConnectionPoolExhausted) as caught:
                await drive_like_fastapi(
                    agen, lambda session: session.execute(text("SELECT 1"))
                )
            # The driver's text reaches the log, never the client.
            assert "QueuePool" in str(caught.value)

    with_tiny_pool(body)


def test_a_free_pool_still_yields_a_working_session():
    """The guard must not fire when the pool has room.

    Cheap, and the obvious way to get this wrong is to translate every checkout.
    """

    async def body(engine: AsyncEngine) -> None:
        agen = get_session()
        session = await agen.__anext__()
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        await agen.aclose()

    with_tiny_pool(body)


def test_the_connection_is_returned_after_a_timeout():
    """A full pool must not become a permanently broken one.

    The failure path runs inside `async with SessionLocal()`, so if translation
    escaped before the context manager unwound, the checkout would leak and the
    pool would stay empty for good -- turning a transient 503 into an outage.
    """

    async def body(engine: AsyncEngine) -> None:
        async with engine.connect():
            agen = get_session()
            with pytest.raises(ConnectionPoolExhausted):
                await drive_like_fastapi(
                    agen, lambda session: session.execute(text("SELECT 1"))
                )

        # The held connection is back; a fresh checkout must now succeed.
        agen = get_session()
        session = await agen.__anext__()
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        await agen.aclose()

    with_tiny_pool(body)


# ── through the API, which is where the 500 was actually seen ────────────────

@pytest.fixture
def authed(client):
    """A real token, minted before the pool is broken.

    Needed because authentication now short-circuits on a bad token *before*
    touching the database: `get_current_user` decodes first and only then opens a
    short-lived session. With a junk token the request never reaches a checkout,
    so a full pool produces a 401 rather than the 503 under test -- which is how
    the first version of these tests failed after the session refactor.

    That is a real improvement worth stating: an invalid token now costs no
    database connection at all.
    """
    import uuid

    email = f"pool-{uuid.uuid4().hex[:10]}@example.com"
    signup = client.post("/auth/signup",
                         json={"email": email, "password": "correct-horse-battery"})
    assert signup.status_code == 201, signup.text
    return {"Authorization": f"Bearer {signup.json()['access_token']}"}


@pytest.fixture
def client():
    """One client for the whole test, with server exceptions surfaced as responses.

    `raise_server_exceptions=False` matters: TestClient re-raises unhandled server
    exceptions by default, so a regression would surface as the exception itself
    instead of the 500 a real client would receive -- and the test would be
    asserting on the wrong thing entirely.

    Deliberately a single client. Opening a second one inside a test that already
    holds this fixture hangs: each TestClient runs the app's lifespan, and the
    inner one's shutdown disposes the engine the outer one is still using.
    """
    with TestClient(app, raise_server_exceptions=False) as c:
        health = c.get("/health/db")
        if health.status_code != 200:
            pytest.skip(f"Postgres unreachable ({health.text}); run migrations first")
        yield c


@pytest.fixture
def pool_is_full(monkeypatch):
    """Make every checkout fail the way a full pool does.

    Raises the real `sqlalchemy.exc.TimeoutError` with the real message, inside
    `get_session`'s try block, so this exercises the translation and the handler
    together. See the module docstring for why this is synthesized rather than
    held open.
    """

    def refuse(*_args, **_kwargs):
        raise PoolTimeout(REAL_POOL_TIMEOUT_MESSAGE)

    monkeypatch.setattr(db_base, "SessionLocal", refuse)


def call_under(client: TestClient, headers: dict):
    """Hit a route that needs the database, with credentials that get that far."""
    return client.get("/conversations", headers=headers)


def test_a_full_pool_returns_503_not_500(client, authed, pool_is_full):
    """The end-to-end claim: saturation is a 503 a client can act on."""
    response = call_under(client, authed)

    assert response.status_code == 503, (
        f"expected 503 for a full pool, got {response.status_code}: "
        f"{response.text[:300]}"
    )
    body = response.json()
    assert "busy" in body["detail"].lower(), body
    assert "retry" in body["detail"].lower(), body
    # Names the ceiling, so the reader learns which number to raise.
    assert str(settings.db_pool_size + settings.db_max_overflow) in body["hint"], body
    # A dead database is not worth retrying in five seconds; a full pool is.
    assert response.headers.get("Retry-After") == "5", dict(response.headers)


def test_the_busy_response_leaks_no_connection_string(client, authed, pool_is_full):
    """The driver's message carries the DSN, so it stays in the log."""
    text_out = call_under(client, authed).text
    for secret in ("postgresql", "asyncpg", "password", settings.database_url):
        assert secret not in text_out, f"{secret!r} leaked into the response body"


def test_the_busy_message_does_not_blame_postgres(client, authed, pool_is_full):
    """The generic hint asks whether Postgres is running.

    Under pool exhaustion Postgres is healthy and answering, and sending someone
    to check the database sends them to the wrong machine. That is the whole
    reason the subclass exists, so it is asserted rather than trusted.
    """
    body = call_under(client, authed).json()
    combined = f"{body['detail']} {body['hint']}".lower()
    assert "is postgres running" not in combined, body
    assert "migrat" not in combined, body


def test_an_unreachable_database_still_reports_itself_as_such(client, authed, monkeypatch):
    """Regression: the new branch must not shadow the original one.

    Both are 503, and it would be easy to collapse them -- but "the database is
    gone" and "we are at capacity" need different actions from whoever reads
    them, which is the entire point of separating them.
    """

    def explode(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(db_base, "SessionLocal", explode)

    response = call_under(client, authed)
    assert response.status_code == 503
    body = response.json()
    assert body["detail"] == "Database unavailable."
    assert "Postgres" in body["hint"]
    # The generic case is not retryable in five seconds, so it promises nothing.
    assert "Retry-After" not in response.headers
