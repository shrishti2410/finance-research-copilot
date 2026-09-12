"""FastAPI dependencies for authenticated routes."""

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from auth.security import decode_access_token
from db.base import session_scope
from db.models import User

# auto_error=False so a missing header reaches our handler and gets the same
# shaped 401 (with WWW-Authenticate) as a bad token, rather than a 403.
_bearer = HTTPBearer(auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated.",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    """Identify the caller, holding a connection only for the lookup itself.

    Deliberately *not* `Depends(get_session)`. A dependency's session lives as
    long as the request, which for `/ask/stream` is the whole streamed answer --
    minutes. Authentication needs the database for one indexed lookup, so pinning
    a pooled connection for the rest of the response was doubling the cost of
    every in-flight question: the load test measured two connections held per
    streaming request, which is why exhaustion began at ten concurrent users
    against a pool of fifteen rather than at sixteen.

    The returned `User` is detached. Safe, and checked: every caller reads only
    `user.id` (plus `is_active`/`password_hash` in `routes_auth`, which loads its
    own row), all of which are column attributes already loaded by the select
    below. `expire_on_commit=False` means closing the session does not expire
    them. A relationship access would raise `DetachedInstanceError`, which is why
    nothing here returns one.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _UNAUTHORIZED

    claims = decode_access_token(credentials.credentials)
    if claims is None:
        raise _UNAUTHORIZED

    try:
        user_id = uuid.UUID(claims["sub"])
    except (ValueError, KeyError):
        raise _UNAUTHORIZED from None

    # The DB lookup is what makes deactivation take effect immediately, even
    # though the token itself stays cryptographically valid until it expires.
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.id == user_id))

    if user is None or not user.is_active:
        raise _UNAUTHORIZED

    return user
