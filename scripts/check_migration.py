"""Verify that the Alembic migrations and the ORM models describe the same schema.

`alembic check` does the same job better, but it needs a live database. This
runs anywhere: it renders DDL from `Base.metadata`, renders DDL from the
migration chain in offline mode, normalizes both, and diffs the statement sets.

    python scripts/check_migration.py

Exit code 0 if they agree, 1 if they have drifted -- so CI can gate on it.

A migration that adds a column to an existing table renders as ALTER TABLE ...
ADD COLUMN, while the models render the table whole. Both sides are therefore
compared as tables: a CREATE TABLE becomes its name and the set of its column
and constraint clauses, and an ADD COLUMN adds its clause to that set. Column
order is not compared; Postgres appends an added column wherever the model
declares it.
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


_CREATE_TABLE = re.compile(r"^CREATE TABLE (\S+) \((.*)\)$", re.S)
_ADD_COLUMN = re.compile(r"^ALTER TABLE (\S+) ADD COLUMN (.*)$", re.S)


def _clauses(body: str) -> list[str]:
    """The comma-separated clauses of a CREATE TABLE body, at the top level only:
    NUMERIC(30, 6) and UNIQUE (a, b) are one clause each."""
    out, depth, current = [], 0, []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            out.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        out.append("".join(current).strip())
    return out


def canonical(statements: list[str]) -> set[str]:
    """Statements with every table written as its name and sorted clauses, and
    each ADD COLUMN folded into the table it alters."""
    tables: dict[str, set[str]] = {}
    others: set[str] = set()
    for stmt in statements:
        created = _CREATE_TABLE.match(stmt)
        added = _ADD_COLUMN.match(stmt)
        if created:
            tables.setdefault(created.group(1), set()).update(_clauses(created.group(2)))
        elif added:
            tables.setdefault(added.group(1), set()).add(added.group(2).strip())
        else:
            others.add(stmt)
    return others | {f"CREATE TABLE {name} ({', '.join(sorted(clauses))})"
                     for name, clauses in tables.items()}


def from_models() -> set[str]:
    statements: list[str] = []
    for table in Base.metadata.sorted_tables:
        statements.append(normalize(str(CreateTable(table).compile(dialect=DIALECT))))
        for index in table.indexes:
            statements.append(normalize(str(CreateIndex(index).compile(dialect=DIALECT))))
    return canonical(statements)


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

    statements: list[str] = []
    for raw in result.stdout.split(";"):
        # Drop the "-- Running upgrade" banners Alembic interleaves.
        body = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("--"))
        stmt = normalize(body)
        if not (stmt.upper().startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX"))
                or _ADD_COLUMN.match(stmt)):
            continue
        if any(f" {name} " in f" {stmt} " for name in IGNORED):
            continue
        statements.append(stmt)
    return canonical(statements)


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
