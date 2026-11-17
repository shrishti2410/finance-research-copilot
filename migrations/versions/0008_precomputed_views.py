"""precomputed_views: stock pages computed once a day

Revision ID: 0008_precomputed_views
Revises: 0007_strategy_cards
Create Date: 2026-11-17

The dashboard and the stock pages, computed after the NSE close by
scripts/precompute_views.py and served by the API when fresh
(market/precompute.py). The job's database role may write this table and
nothing else (deploy/neon-precompute-role.sql). Model:
db.models.PrecomputedView.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_precomputed_views"
down_revision = "0007_strategy_cards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "precomputed_views",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=True),
        sa.Column("code_version", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("precomputed_views")
