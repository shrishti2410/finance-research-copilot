"""Require pgvector: convert a real[] embedding column, and ensure the HNSW index

Revision ID: 0005_pgvector_required
Revises: 0004_public_demo
Create Date: 2026-11-16

Migration 0002 fell back to a `real[]` column on a server without pgvector.
rag/vector_store.py no longer searches that column (see its docstring), so this
migration brings an existing database onto pgvector:

- column already `vector`: only make sure the HNSW index exists;
- column `real[]` and pgvector installable: install it, convert the column in
  place (pgvector casts real[] to vector), and build the index;
- column `real[]` and no pgvector on the server: stop, with the fix.

The conversion keeps every stored embedding: the values are the same numbers in
a different type, so nothing needs re-embedding.
"""

from alembic import context, op

revision = "0005_pgvector_required"
down_revision = "0004_public_demo"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 384


def upgrade() -> None:
    if context.is_offline_mode():
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute("CREATE INDEX IF NOT EXISTS ix_filing_chunks_embedding_hnsw "
                   "ON filing_chunks USING hnsw (embedding vector_cosine_ops)")
        return

    bind = op.get_bind()
    column = bind.exec_driver_sql(
        "SELECT udt_name FROM information_schema.columns "
        "WHERE table_name = 'filing_chunks' AND column_name = 'embedding'"
    ).scalar()
    if column != "vector":
        available = bind.exec_driver_sql(
            "SELECT COUNT(*) FROM pg_available_extensions WHERE name = 'vector'"
        ).scalar()
        if not available:
            raise RuntimeError(
                "filing_chunks.embedding is real[] and this Postgres server has no "
                "pgvector extension. Run Postgres with pgvector (the pgvector/pgvector "
                "Docker image; docs/DATABASE.md) and migrate again."
            )
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute(f"ALTER TABLE filing_chunks ALTER COLUMN embedding "
                   f"TYPE vector({EMBEDDING_DIM}) USING embedding::vector({EMBEDDING_DIM})")
    op.execute("CREATE INDEX IF NOT EXISTS ix_filing_chunks_embedding_hnsw "
               "ON filing_chunks USING hnsw (embedding vector_cosine_ops)")


def downgrade() -> None:
    # The index and the vector type stay: going back to real[] would bring back
    # the path this migration exists to remove.
    pass
