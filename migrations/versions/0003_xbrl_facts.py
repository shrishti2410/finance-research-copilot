"""xbrl_facts: reported figures by company, concept and fiscal period

Revision ID: 0003_xbrl_facts
Revises: 0002_filing_chunks
Create Date: 2026-11-15

Structured financial facts from SEC's XBRL company-facts API, so a question
about a reported number is a lookup by (company, concept, fiscal year) rather
than a search for the right cell of a rendered table. The model is
`db.models.XbrlFact`; `ingestion/xbrl_facts.py` says how the rows are chosen.
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_xbrl_facts"
down_revision = "0002_filing_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "xbrl_facts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("cik", sa.String(length=10), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("taxonomy", sa.String(length=32), nullable=False),
        sa.Column("concept", sa.String(length=255), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("fiscal_period", sa.String(length=4), server_default=sa.text("'FY'"),
                  nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(precision=30, scale=6), nullable=False),
        sa.Column("accession", sa.String(length=25), nullable=False),
        sa.Column("form", sa.String(length=12), nullable=False),
        sa.Column("filed", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cik", "taxonomy", "concept", "unit", "fiscal_year",
                            "fiscal_period", name="uq_xbrl_facts_company_concept_period"),
    )
    op.create_index("ix_xbrl_facts_ticker_concept_year", "xbrl_facts",
                    ["ticker", "concept", "fiscal_year"])


def downgrade() -> None:
    op.drop_index("ix_xbrl_facts_ticker_concept_year", table_name="xbrl_facts")
    op.drop_table("xbrl_facts")
