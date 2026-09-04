"""Verify that the Alembic migrations and the ORM models describe the same schema.

`alembic check` does the same job better, but it needs a live database. This
runs anywhere: it renders DDL from `Base.metadata`, renders DDL from the
migration chain in offline mode, normalizes both, and diffs the statement sets.

    python scripts/check_migration.py

Exit code 0 if they agree, 1 if they have drifted -- so CI can gate on it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

import db.models  # noqa: F401,E402  (registers the tables on Base.metadata)
from db.base import Base  # noqa: E402

DIALECT = postgresql.dialect()

# Tables with no ORM model, so their absence from Base.metadata is not drift.
#   alembic_version   Alembic's own bookkeeping.
#   filing_chunks     raw DDL in migration 0002. Its embedding column is
#                     vector(384) or real[] depending on whether pgvector is
#                     installed on the target server, which is not something
#                     Base.metadata can express.
IGNORED = {"alembic_version", "filing_chunks"}


def normalize(statement: str) -> str:
    """Collapse whitespace so formatting differences are not reported as drift."""
    return re.sub(r"\s+", " ", statement).strip().rstrip(";").strip()


def from_models() -> set[str]:
    statements: set[str] = set()
    for table in Base.metadata.sorted_tables:
        statements.add(normalize(str(CreateTable(table).compile(dialect=DIALECT))))
        for index in table.indexes:
            statements.add(normalize(str(CreateIndex(index).compile(dialect=DIALECT))))
    return statements


def from_migrations() -> set[str]:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("alembic failed to render offline SQL:\n" + result.stderr, file=sys.stderr)
        raise SystemExit(1)

    statements: set[str] = set()
    for raw in result.stdout.split(";"):
        # Drop the "-- Running upgrade" banners Alembic interleaves.
        body = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("--"))
        stmt = normalize(body)
        if not stmt.upper().startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")):
            continue
        if any(f" {name} " in f" {stmt} " for name in IGNORED):
            continue
        statements.add(stmt)
    return statements


def main() -> int:
    models = from_models()
    migrations = from_migrations()

    only_in_models = models - migrations
    only_in_migrations = migrations - models

    if not only_in_models and not only_in_migrations:
        print(f"OK  models and migrations agree ({len(models)} statements).")
        return 0

    print("DRIFT between db/models.py and migrations/\n")
    for stmt in sorted(only_in_models):
        print(f"  in models, missing from migrations:\n    {stmt}\n")
    for stmt in sorted(only_in_migrations):
        print(f"  in migrations, missing from models:\n    {stmt}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
