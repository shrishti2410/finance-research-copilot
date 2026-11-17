"""Create the precompute job's Neon role and store its connection string as a
GitHub secret, without either being shown. Run it yourself:

    python scripts/create_precompute_role.py             # create (or rotate), check, set the secret
    python scripts/create_precompute_role.py --no-secret --env-file .env.pgvector
                                                         # rehearse on the local database

What it does, in order:

1. Reads the owner connection string from .env.neon (DATABASE_URL). Not printed.
2. Generates a 32-byte random password in memory.
3. As the owner: CREATE ROLE signals_precompute LOGIN (or ALTER ... PASSWORD
   if it exists), then runs deploy/neon-precompute-role.sql.
4. Connects as the new role and checks it can read the price cache and the
   cards, write precomputed_views, and is refused on users and on writing
   price_bars. Prints pass or fail per check.
5. Pipes the role's connection string to `gh secret set
   NEON_PRECOMPUTE_DATABASE_URL` on stdin. Needs the GitHub CLI, logged in.

Needs migration 0008 on Neon first (precomputed_views must exist).
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
ROLE = "signals_precompute"
SECRET = "NEON_PRECOMPUTE_DATABASE_URL"


def owner_url(env_file: Path) -> str:
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().replace("postgresql+asyncpg://", "postgresql://")
    sys.exit(f"No DATABASE_URL in {env_file.name}")


def role_url(owner: str, password: str) -> str:
    parts = urlsplit(owner)
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, f"{ROLE}:{quote(password, safe='')}@{host}",
                       parts.path, parts.query, ""))


async def connect(url: str):
    local = urlsplit(url).hostname in ("127.0.0.1", "localhost")
    return await asyncpg.connect(url, ssl=None if local else ssl.create_default_context(),
                                 timeout=30)


async def verify(url: str) -> bool:
    conn = await connect(url)
    ok = True

    async def check(label, sql, should_work):
        nonlocal ok
        try:
            await conn.execute(sql)
            worked = True
        except asyncpg.PostgresError:
            worked = False
        good = worked == should_work
        ok &= good
        print(f"  {'pass' if good else 'FAIL'}  {label}")

    try:
        await check("reads price_bars", "SELECT 1 FROM price_bars LIMIT 1", True)
        await check("reads strategy_cards", "SELECT 1 FROM strategy_cards LIMIT 1", True)
        await check("writes precomputed_views (rolled back)",
                    "BEGIN; INSERT INTO precomputed_views (key, code_version, payload) "
                    "VALUES ('_grant_check', 'x', '{}') ON CONFLICT (key) DO UPDATE "
                    "SET payload = EXCLUDED.payload; ROLLBACK;", True)
        if conn.is_in_transaction():
            await conn.execute("ROLLBACK")
        await check("refused: reading users", "SELECT 1 FROM users LIMIT 1", False)
        await check("refused: writing price_bars",
                    "INSERT INTO price_bars (ticker, day, open, high, low, close, adj_close, "
                    "volume, provider) VALUES ('_X', '2000-01-03', 1, 1, 1, 1, 1, 1, 'x')", False)
        await check("refused: creating a table", "CREATE TABLE _probe (x int)", False)
    finally:
        await conn.close()
    return ok


async def main(env_file: Path, set_secret: bool) -> int:
    owner = owner_url(env_file)
    password = secrets.token_urlsafe(32)
    conn = await connect(owner)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", ROLE)
        verb = "ALTER" if exists else "CREATE"
        # The password is a literal in the statement (DDL takes no parameters);
        # token_urlsafe never contains a quote.
        await conn.execute(f"{verb} ROLE {ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB "
                           f"NOCREATEROLE NOINHERIT PASSWORD '{password}'")
        await conn.execute((ROOT / "deploy/neon-precompute-role.sql").read_text(encoding="utf-8"))
        print(f"role {ROLE}: {verb.lower()}d, grants applied")
    finally:
        await conn.close()
    url = role_url(owner, password)
    print("checks, connected as the new role:")
    if not await verify(url):
        print("Some checks failed; the secret was not set.")
        return 1
    if not set_secret:
        print("--no-secret: the role is ready; no GitHub secret was set.")
        return 0
    done = subprocess.run(["gh", "secret", "set", SECRET], input=url, text=True,
                          capture_output=True)
    if done.returncode != 0:
        print(f"gh secret set failed: {done.stderr.strip()[:200]}")
        return 1
    print(f"GitHub secret {SECRET} set (value not shown).")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env.neon",
                    help="where the owner's DATABASE_URL is (default .env.neon)")
    ap.add_argument("--no-secret", action="store_true", help="do not call gh secret set")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.env_file, not args.no_secret)))
