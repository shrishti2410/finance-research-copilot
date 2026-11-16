"""price_bars and price_fetches: a Postgres cache of daily prices

Revision ID: 0006_price_bars
Revises: 0005_pgvector_required
Create Date: 2026-11-16

Version 2's market-data layer (market/): daily bars from a provider, cached so
an indicator or a chart does not refetch history it already has. The models
are db.models.PriceBar and db.models.PriceFetch.
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_price_bars"
down_revision = "0005_pgvector_required"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_bars",
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("adj_close", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("ticker", "day"),
    )
    op.create_table(
        "price_fetches",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("start_day", sa.Date(), nullable=False),
        sa.Column("end_day", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("bars", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_price_fetches_ticker", "price_fetches", ["ticker"])


def downgrade() -> None:
    op.drop_index("ix_price_fetches_ticker", table_name="price_fetches")
    op.drop_table("price_fetches")
    op.drop_table("price_bars")
