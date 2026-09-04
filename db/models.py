"""ORM models: users, conversations, messages.

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
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
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
