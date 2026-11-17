"""filing_chunks: a full-text column and index for hybrid retrieval

Revision ID: 0009_filing_chunks_fts
Revises: 0008_precomputed_views
Create Date: 2026-11-17

A generated tsvector over the chunk text ('english' stemming and stop words)
and a GIN index on it, for the full-text half of hybrid retrieval
(rag/vector_store.py `text_search`, fused with the vector ranking in
rag/retriever.py). Generated and stored, so every existing and future chunk
has it without a backfill or a change to the indexer.

Raw DDL, like the table itself (migration 0002): filing_chunks has no ORM model.
"""

from alembic import op

revision = "0009_filing_chunks_fts"
down_revision = "0008_precomputed_views"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE filing_chunks
        ADD COLUMN content_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED
    """)
    op.execute("CREATE INDEX ix_filing_chunks_content_tsv ON filing_chunks USING gin (content_tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_filing_chunks_content_tsv")
    op.execute("ALTER TABLE filing_chunks DROP COLUMN IF EXISTS content_tsv")
