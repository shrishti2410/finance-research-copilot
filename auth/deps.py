"""FastAPI dependencies for authenticated routes."""

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.security import decode_access_token
from db.base import get_session
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
    session: AsyncSession = Depends(get_session),
) -> User:
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
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None or not user.is_active:
        raise _UNAUTHORIZED

    return user
