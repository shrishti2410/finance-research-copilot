"""usage_counters and users.is_demo: daily caps for a public deployment

Revision ID: 0004_public_demo
Revises: 0003_xbrl_facts
Create Date: 2026-11-16

A demo account flag, and per-day counters of questions and hosted-model tokens,
so a public deployment can cap what visitors spend. See api/quotas.py.
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_public_demo"
down_revision = "0003_xbrl_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_demo", sa.Boolean(), server_default=sa.text("false"),
                                     nullable=False))
    op.create_table(
        "usage_counters",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("questions", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("day", "scope", "key"),
    )


def downgrade() -> None:
    op.drop_table("usage_counters")
    op.drop_column("users", "is_demo")
