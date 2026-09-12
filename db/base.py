"""Async engine, session factory, and the declarative Base.

One engine per process, created at import time. SQLAlchemy does not connect
until the first checkout, so this stays import-safe without a live server --
which is what lets `alembic --sql` and unit tests import the models.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from core.config import settings


class DatabaseUnavailable(Exception):
    """The database could not be reached.

    Distinct from a query or constraint error, which means the database answered
    and said no. `api/main.py` maps this to 503; everything else stays a 500.
    """


class ConnectionPoolExhausted(DatabaseUnavailable):
    """Every pooled connection is checked out and the wait timed out.

    A subclass, so the existing 503 handler catches it without a second
    registration -- Starlette resolves handlers along the exception's MRO. It is
    still its own type because the *cause* is the opposite of its parent's:
    Postgres is healthy and answering, and this process is simply holding every
    connection it is allowed to open. "Is Postgres running?" is the wrong
    question to put in front of someone debugging this.

    Found by the load test in `benchmarks/LOAD_TEST_RESULTS.md`. `/ask` and
    `/ask/stream` hold their session for the whole agent run -- minutes, nearly
    all of it waiting on the model -- so `db_pool_size + db_max_overflow`
    connections is the real concurrency ceiling of the deployment. At ten
    concurrent users it produced 25 pool timeouts and 33 unhandled 500s.
    """


class Base(DeclarativeBase):
    """Declarative base. `Base.metadata` is what Alembic autogenerates against."""


engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    # Postgres behind a proxy or NAT drops idle connections without telling the
    # pool; without pre_ping the first request after an idle period fails.
    pool_pre_ping=True,
)

# expire_on_commit=False is load-bearing under asyncio. The default expires every
# attribute on commit, so reading `user.email` afterwards triggers a lazy refresh
# -- blocking IO from a context that cannot await, which raises MissingGreenlet.
SessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session for one unit of work, released as soon as the block ends.

    The same error translation as `get_session`, factored out so a handler that
    manages its own session lifetime does not silently lose it. Using
    `SessionLocal()` directly would: a pool timeout would stop being a 503 and go
    back to being the unhandled 500 that `tests/test_pool_exhaustion.py` exists to
    prevent.

    Use this wherever a connection should be held for a query rather than for a
    request -- `api/routes.py` holds one to read history and again to write the
    turn, and none at all across the minutes of inference in between.
    """
    try:
        async with SessionLocal() as session:
            yield session
    except PoolTimeout as exc:
        raise ConnectionPoolExhausted(str(exc)) from exc
    except (OperationalError, InterfaceError, OSError) as exc:
        raise DatabaseUnavailable(str(exc)) from exc


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. One session per request, always closed.

    Connection failures are translated here rather than in a global exception
    handler, so the translation is scoped to actual database work. asyncpg
    raises a bare ConnectionRefusedError when nothing is listening -- the
    failure happens before a DBAPI connection exists, so SQLAlchemy never gets
    to wrap it in OperationalError, and a handler registered for the SQLAlchemy
    exceptions alone would silently miss the most common case.

    The route body runs at the `yield`, so its exceptions arrive here too
    (FastAPI throws them back into the dependency generator).
    """
    # Delegates, so the translation exists once. An exception raised in the route
    # body is thrown back in at this `yield`, propagates into `session_scope`'s
    # own yield, and is caught by its clauses -- same behaviour as when those
    # clauses were written out here.
    async with session_scope() as session:
        yield session


async def dispose_engine() -> None:
    """Close the pool on shutdown so connections are returned, not reset by peer."""
    await engine.dispose()
