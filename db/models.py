"""ORM models: users, conversations, messages, and the XBRL facts.

The full write-up is in docs/DATABASE.md. The short version of the one decision
that looks inconsistent on purpose:

  * users and conversations use UUID primary keys, because those ids travel in
    JWTs and URLs, where a sequential integer leaks how many rows exist and
    invites enumeration.
  * messages use a BIGINT identity key, because messages are the append-heavy
    table and are never addressed globally -- a message is always reached
    through a conversation whose UUID already did the security work. A monotonic
    key keeps the B-tree packed and doubles as a total ordering and a cursor.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

# Kept in sync with the CHECK constraint on messages.role below.
MESSAGE_ROLES = ("user", "assistant", "system", "tool")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,                        # object has an id before flush
        server_default=text("gen_random_uuid()"),  # core since PG13, no pgcrypto
    )
    # Stored already lower-cased and stripped by the API layer, so a plain UNIQUE
    # index is enough. That keeps every later comparison a plain equality: no
    # citext extension to install per environment, no functional index on
    # lower(email) that every query has to remember to match.
    # 320 = RFC 5321 maximum (64-char local part + "@" + 255-char domain).
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100))
    # Disable rather than delete: deleting a user cascades their history away, so
    # that should be a deliberate, separate operation.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # A throwaway account handed out by POST /auth/demo. Its daily question cap
    # is the demo's, not a regular account's. See api/quotas.py.
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,  # let the DB's ON DELETE CASCADE do it, don't load children
        lazy="raise",          # a lazy load here would be blocking IO inside async
    )

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        # Matches the thread-list query exactly: this user's conversations,
        # most recently active first.
        Index("ix_conversations_user_id_updated_at", "user_id", text("updated_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # Ownership is a column, not an assumption. There is no read path in this
    # codebase that fetches a conversation by id alone.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable because it is derived from the first user message. NULL is honest
    # about "not known yet" in a way an empty string is not.
    title: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Denormalized deliberately. Ordering threads by activity otherwise needs
    # MAX(messages.created_at) grouped per conversation -- an aggregate over the
    # largest table on every page load. One extra column write per message
    # replaces that with an index scan.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="conversations", lazy="raise")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
        order_by="Message.id",
    )

    def __repr__(self) -> str:
        return f"<Conversation {self.id} {self.title!r}>"


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')", name="ck_messages_role"
        ),
        # Serves both the history read and its keyset pagination:
        # WHERE conversation_id = ? AND id > ? ORDER BY id.
        Index("ix_messages_conversation_id_id", "conversation_id", "id"),
    )

    # BIGINT identity, not UUID -- see the module docstring. It is also a total
    # order: two rows can share a created_at down to the microsecond, and a tie
    # makes a page boundary non-deterministic. An identity column cannot tie.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # VARCHAR + CHECK rather than a native PG ENUM. Adding a value to an enum is
    # an ALTER TYPE with awkward transactional semantics; adding one to a CHECK
    # is a constraint swap Alembic writes cleanly. This value set will grow.
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # Inline, not a side table -- Postgres TOASTs oversized text on its own.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Role-dependent extras that should not each become a nullable column:
    # citations for a grounded answer, token counts, model name, TTFT. Named
    # `meta` because `metadata` is reserved on SQLAlchemy's declarative Base.
    meta: Mapped[dict | None] = mapped_column("meta", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="messages", lazy="raise")

    def __repr__(self) -> str:
        return f"<Message {self.id} {self.role}>"


class PriceBar(Base):
    """One trading day of prices, cached from a market-data provider.

    `close` is split-adjusted, `adj_close` also dividend-adjusted; see
    market/providers.py for which one each calculation uses. Keyed by ticker
    and day: one provider at a time feeds the cache, and `provider` says which.
    """

    __tablename__ = "price_bars"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    adj_close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)


class PriceFetch(Base):
    """A range the cache has asked a provider for, and when.

    What makes "no bar for that day" mean a holiday rather than "never asked":
    a request is served from the cache when one fetch covers it and is younger
    than MARKET_CACHE_TTL_HOURS. See market/store.py.
    """

    __tablename__ = "price_fetches"
    __table_args__ = (Index("ix_price_fetches_ticker", "ticker"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    start_day: Mapped[date] = mapped_column(Date, nullable=False)
    end_day: Mapped[date] = mapped_column(Date, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    bars: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PrecomputedView(Base):
    """A stock page or the dashboard, computed once a day (market/precompute.py).

    `payload` is the endpoint's JSON; `code_version` is the hash of the signal
    code that produced it, so a page from older code is never served.
    """

    __tablename__ = "precomputed_views"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    code_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UsageCounter(Base):
    """Questions and hosted-model tokens per UTC day, for the caps in api/quotas.py.

    `scope` says what is counted ("demo_account", "demo_all", "user",
    "hosted_tokens") and `key` whose ("all", or a user id). Written only by
    conditional UPSERTs, so the primary key is the whole identity of a row.
    """

    __tablename__ = "usage_counters"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    questions: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))


class XbrlFact(Base):
    """One reported number: a company, a concept, a unit and a fiscal period.

    Filled from SEC's company-facts API by `scripts/index_facts.py`; see
    `ingestion/xbrl_facts.py` for which of the several reported versions of a
    number is the one kept. The agent's `get_financial_fact` tool reads it.

    The key is what a question names -- "NVIDIA's diluted EPS for fiscal 2024"
    is (company, concept, fiscal year) -- so that is the unique constraint, and
    re-running the ingestion replaces a row rather than adding a second one.
    """

    __tablename__ = "xbrl_facts"
    __table_args__ = (
        UniqueConstraint(
            "cik", "taxonomy", "concept", "unit", "fiscal_year", "fiscal_period",
            name="uq_xbrl_facts_company_concept_period",
        ),
        # The tool's lookup: WHERE ticker = ? AND concept IN (...) AND fiscal_year = ?
        Index("ix_xbrl_facts_ticker_concept_year", "ticker", "concept", "fiscal_year"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    # SEC's company id, zero-padded to ten digits. The stable key: a ticker can
    # change, a CIK does not.
    cik: Mapped[str] = mapped_column(String(10), nullable=False)
    ticker: Mapped[str] = mapped_column(String(12), nullable=False)
    # "us-gaap", "dei", "ifrs-full". Part of the key because two taxonomies may
    # each define a concept of the same name.
    taxonomy: Mapped[str] = mapped_column(String(32), nullable=False)
    concept: Mapped[str] = mapped_column(String(255), nullable=False)
    # "USD", "shares", "USD/shares". A concept can be reported in more than one.
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    # The company's own label for the year: NVIDIA's fiscal 2026 ended in
    # January 2026. Derived from the period end, not from the filing's `fy`.
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    # "FY" today. A column rather than an assumption so quarters can be added
    # without a migration.
    fiscal_period: Mapped[str] = mapped_column(String(4), nullable=False,
                                               server_default=text("'FY'"))
    # NULL for a balance on a day (total assets); set for a flow (revenue).
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    # NUMERIC, not float: these are reported figures, and 215938000000 must come
    # back as exactly that. Six decimals covers per-share amounts.
    value: Mapped[Decimal] = mapped_column(Numeric(30, 6), nullable=False)
    # The filing this figure was taken from, e.g. 0001045810-26-000021. It is
    # the citation the tool returns.
    accession: Mapped[str] = mapped_column(String(25), nullable=False)
    form: Mapped[str] = mapped_column(String(12), nullable=False)
    filed: Mapped[date] = mapped_column(Date, nullable=False)

    def __repr__(self) -> str:
        return f"<XbrlFact {self.ticker} {self.concept} FY{self.fiscal_year} {self.value}>"
