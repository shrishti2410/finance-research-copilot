"""Conversations and messages.

    POST /conversations                    start a thread
    GET  /conversations                    list the caller's threads, recent first
    POST /conversations/{id}/messages      append a message
    GET  /conversations/{id}/messages      read history (keyset paginated)

Every handler is scoped to the authenticated user by `owned_conversation`,
which POST /ask reuses.
There is no route that reads a conversation without that check.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.schemas import (
    ConversationCreate,
    ConversationOut,
    MessageCreate,
    MessageOut,
    MessagePage,
)
from auth.deps import get_current_user
from db.base import get_session
from db.models import Conversation, Message, User

router = APIRouter(prefix="/conversations", tags=["conversations"])

TITLE_MAX = 200


async def owned_conversation(
    conversation_id: uuid.UUID, user: User, session: AsyncSession
) -> Conversation:
    """Fetch a conversation, or 404.

    404 rather than 403 for someone else's conversation. A 403 confirms the id
    exists, which turns the endpoint into an existence oracle for other people's
    data; from outside, "not yours" and "not there" should be indistinguishable.
    """
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return conversation


def append_message(
    session: AsyncSession,
    conversation: Conversation,
    role: str,
    content: str,
    meta: dict | None = None,
) -> Message:
    """Add a message and bump the thread's activity. Does not commit.

    Shared with POST /ask, which writes two messages around an agent run and
    must land them in one transaction -- a user message persisted without the
    answer that followed it is a thread that looks unanswered.

    updated_at is set explicitly rather than left to `onupdate`, which fires
    only when the ORM emits an UPDATE for the conversation row, and adding a
    child row is not one.
    """
    message = Message(
        conversation_id=conversation.id, role=role, content=content, meta=meta
    )
    session.add(message)

    conversation.updated_at = datetime.now(timezone.utc)
    if conversation.title is None and role == "user":
        conversation.title = _derive_title(content)
    return message


def _derive_title(content: str) -> str:
    """First line of the opening message, clipped. Placeholder until the agent
    can summarize a thread properly."""
    first_line = content.strip().splitlines()[0] if content.strip() else "New conversation"
    return first_line[: TITLE_MAX - 1] + "…" if len(first_line) > TITLE_MAX else first_line


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Conversation:
    conversation = Conversation(user_id=user.id, title=body.title)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)  # pick up server-side defaults (id, timestamps)
    return conversation


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Conversation]:
    # Exactly the shape ix_conversations_user_id_updated_at was built for.
    result = await session.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    )
    return list(result)


@router.post(
    "/{conversation_id}/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED
)
async def post_message(
    conversation_id: uuid.UUID,
    body: MessageCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Message:
    conversation = await owned_conversation(conversation_id, user, session)

    message = append_message(session, conversation, body.role, body.content, body.meta)

    # One commit, so the message and the activity bump land in the same
    # transaction -- a thread can never sort as active without the message that
    # made it active being visible.
    await session.commit()
    await session.refresh(message)
    return message


@router.get("/{conversation_id}/messages", response_model=MessagePage)
async def get_history(
    conversation_id: uuid.UUID,
    after_id: int | None = Query(None, ge=0, description="cursor: return messages with id > this"),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MessagePage:
    await owned_conversation(conversation_id, user, session)

    stmt = select(Message).where(Message.conversation_id == conversation_id)
    if after_id is not None:
        stmt = stmt.where(Message.id > after_id)
    # Ordered by id, not created_at: identity values cannot tie, so the page
    # boundary is deterministic even for messages written in the same instant.
    stmt = stmt.order_by(Message.id).limit(limit)

    messages = list(await session.scalars(stmt))
    # A full page means there may be more; a short page is definitively the end.
    next_cursor = messages[-1].id if len(messages) == limit else None
    return MessagePage(
        messages=[MessageOut.model_validate(m) for m in messages], next_cursor=next_cursor
    )
