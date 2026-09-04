"""Password hashing and JWT encode/decode. No FastAPI, no database.

Tokens are stateless access tokens, HS256, short-lived. The trade-off that
implies is real and worth stating plainly: a stateless JWT cannot be revoked
before it expires. Logging out, changing a password, or banning an account does
not invalidate tokens already issued. `jwt_expire_minutes` bounds that window;
closing it properly needs a refresh-token table with rotation and a revocation
list, which is a separate piece of work rather than something to half-build here.
"""

import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from core.config import settings

# bcrypt silently truncates the input at 72 bytes. Callers must reject longer
# passwords rather than accept one whose tail is quietly ignored -- otherwise
# two different long passwords can authenticate the same account.
BCRYPT_MAX_BYTES = 72

_pwd_context = CryptContext(
    schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=settings.bcrypt_rounds
)

# Verified against a throwaway hash when the email does not exist, so that a
# miss costs the same wall-clock time as a hit. Without it, response latency
# tells an attacker which addresses are registered.
_DUMMY_HASH = _pwd_context.hash("not-a-real-password")


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _pwd_context.verify(password, password_hash)
    except ValueError:
        # Malformed/legacy hash in the column: treat as a failed login, not a 500.
        return False


def dummy_verify(password: str) -> None:
    """Burn one bcrypt round so a nonexistent user costs what a real one does."""
    _pwd_context.verify(password, _DUMMY_HASH)


def create_access_token(subject: str) -> tuple[str, int]:
    """Return (token, seconds_until_expiry) for the given user id."""
    now = datetime.now(timezone.utc)
    expires_in = settings.jwt_expire_minutes * 60
    claims = {
        "sub": subject,
        "typ": "access",           # so a refresh token can never be spent as an access token
        "jti": str(uuid.uuid4()),  # the handle a revocation list would key on
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def decode_access_token(token: str) -> dict | None:
    """Return the claims, or None if the token is invalid, expired, or the wrong type.

    `algorithms=` is pinned to the configured algorithm on purpose: accepting
    whatever the token's own header asks for is the classic JWT confusion bug
    (an attacker sends alg=none, or signs an HS256 token with a public RSA key).
    """
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    if claims.get("typ") != "access" or not claims.get("sub"):
        return None
    return claims
