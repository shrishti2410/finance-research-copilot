"""Create a regular (non-demo) account directly in the database.

    python scripts/create_user.py you@yourdomain.com [--name "Your Name"]

For a deployment with SIGNUP_ENABLED=false, where the sign-up form is closed
but the owner still needs an account. The password is asked for twice at a
prompt that does not echo, hashed with the app's own bcrypt settings
(auth/security.py), and never printed or written anywhere else.

The database is whatever DATABASE_URL says, so point it at the hosted one for
this command only (PowerShell):

    $env:DATABASE_URL = ((Get-Content .env.neon) -match '^DATABASE_URL=')[0].Substring(13)
    python scripts/create_user.py you@yourdomain.com
    Remove-Item Env:DATABASE_URL

An existing email is refused, not overwritten.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

MIN_PASSWORD = 8


def read_password() -> str:
    from auth.security import BCRYPT_MAX_BYTES

    first = getpass.getpass("Password (not shown): ")
    if len(first) < MIN_PASSWORD:
        sys.exit(f"The password must be at least {MIN_PASSWORD} characters.")
    if len(first.encode("utf-8")) > BCRYPT_MAX_BYTES:
        sys.exit(f"The password must be at most {BCRYPT_MAX_BYTES} bytes (a bcrypt limit).")
    if getpass.getpass("Again: ") != first:
        sys.exit("The two passwords differ. Nothing was created.")
    return first


async def create(email: str, name: str | None, password: str) -> str:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from auth.security import hash_password
    from core.config import settings

    # Its own engine, closed at the end: a one-shot command has no use for the
    # app's shared pool.
    engine = create_async_engine(settings.database_url, poolclass=NullPool,
                                 connect_args=settings.database_connect_args)
    host = settings.database_url.split("@")[-1].split("/")[0]
    print(f"Database host: {host}")
    async with engine.begin() as conn:
        exists = (await conn.execute(text("SELECT 1 FROM users WHERE email = :e"),
                                     {"e": email})).first()
        if exists:
            await engine.dispose()
            sys.exit(f"{email} already has an account. Nothing was changed.")
        user_id = (await conn.execute(text(
            "INSERT INTO users (email, password_hash, display_name) "
            "VALUES (:e, :h, :n) RETURNING id"),
            {"e": email, "h": hash_password(password), "n": name})).scalar_one()
    await engine.dispose()
    return str(user_id)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("email")
    ap.add_argument("--name", default=None)
    args = ap.parse_args()
    email = args.email.strip().lower()             # the app stores emails lower-cased
    if "@" not in email:
        sys.exit("That is not an email address.")
    password = read_password()
    user_id = asyncio.run(create(email, args.name, password))
    print(f"Created {email} (id {user_id}). Sign in with it on the site's sign-in page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
