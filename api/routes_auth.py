"""Signup, login, and identity.

    GET  /auth/options  what this deployment offers: signup, the demo
    POST /auth/signup   create an account, return an access token
    POST /auth/login    exchange credentials for an access token
    POST /auth/demo     a fresh demo account, and its access token
    GET  /auth/me       the caller's own record
"""

import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.schemas import LoginRequest, SignupRequest, TokenResponse, UserOut
from auth.deps import get_current_user
from auth.security import create_access_token, dummy_verify, hash_password, verify_password
from core.config import settings
from db.base import get_session
from db.models import User

router = APIRouter(prefix="/auth", tags=["auth"])

_BAD_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    # Deliberately does not say which half was wrong -- "no such user" and "wrong
    # password" as separate messages turn the login form into an account oracle.
    detail="Incorrect email or password.",
    headers={"WWW-Authenticate": "Bearer"},
)


# Demo accounts live under this domain, which signup refuses, so a visitor
# cannot register an address that would read as a demo account.
DEMO_EMAIL_DOMAIN = "demo.example.com"


@router.get("/options")
async def options() -> dict:
    """What the sign-in page should offer. No secrets, no caps' current state."""
    return {
        "signup": settings.signup_enabled,
        "demo": settings.demo_enabled,
        "demo_questions_per_account": settings.demo_questions_per_account
        if settings.demo_enabled else None,
    }


@router.post("/demo", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def demo(session: AsyncSession = Depends(get_session)) -> TokenResponse:
    """A new demo account for this visitor, and its token.

    One account per visit, not one shared account: a shared one would show
    every visitor everyone else's conversations. Its password is random and
    never returned, so it can only be used through the token. What a demo
    account may spend is capped in api/quotas.py; how fast this endpoint can
    be called is capped by the /auth rate limit.
    """
    if not settings.demo_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    user = User(
        email=f"demo-{uuid.uuid4().hex[:16]}@{DEMO_EMAIL_DOMAIN}",
        password_hash=hash_password(secrets.token_urlsafe(32)),
        display_name="Demo",
        is_demo=True,
    )
    session.add(user)
    await session.commit()
    token, expires_in = create_access_token(str(user.id))
    return TokenResponse(access_token=token, expires_in=expires_in)


def _normalize(email: str) -> str:
    """Single place where email canonicalization happens.

    Because it happens here, the column can carry a plain UNIQUE index and every
    query downstream is a plain equality. If this ever gains a rule (stripping
    Gmail dots, say), it gains it in exactly one place.
    """
    return email.strip().lower()


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def signup(body: SignupRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    if not settings.signup_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Signup is closed on this deployment."
                   + (" Use the demo instead." if settings.demo_enabled else ""),
        )
    if _normalize(body.email).endswith("@" + DEMO_EMAIL_DOMAIN):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="That email domain is reserved.")
    user = User(
        email=_normalize(body.email),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        # Let the UNIQUE constraint decide, rather than SELECT-then-INSERT: two
        # concurrent signups for the same address both pass a prior existence
        # check and one still has to lose here.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That email is already registered."
        ) from None

    token, expires_in = create_access_token(str(user.id))
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    user = await session.scalar(select(User).where(User.email == _normalize(body.email)))

    if user is None:
        # Spend the same bcrypt time an existing account would, so the 401 for an
        # unknown address is not measurably faster than one for a wrong password.
        dummy_verify(body.password)
        raise _BAD_CREDENTIALS

    if not verify_password(body.password, user.password_hash):
        raise _BAD_CREDENTIALS

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled.")

    token, expires_in = create_access_token(str(user.id))
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
