"""Conversation memory: the prior turns a follow-up question needs to make sense.

    history = await load_history(session, conversation.id)
    result = await run_agent("What about Apple's?", history=history)

Without this, every /ask is turn one. "What about Apple's?" reaches the model
with no antecedent, and the model does the only thing it can -- guesses, or asks
what the user means. The fix is not clever: read the last few messages back out
of Postgres and put them in front of the question.

Why a message window and not a summary
--------------------------------------
Two turns of chat are a few hundred tokens. Summarizing them would cost another
model call, lose the exact wording a reference resolves against ("that", "last
quarter", the ticker named three messages ago), and introduce a second thing
that can be wrong. A window is the right tool until threads grow past what the
context can hold; `WINDOW_MESSAGES` is where that decision gets revisited, and
the docstring on `load_history` says what to do then.

What is deliberately left out
-----------------------------
Tool calls and their results are *not* replayed. Only the user's questions and
the assistant's answers come back. Two reasons: the stored trace is a record of
what happened, not a valid OpenAI message sequence -- replaying tool messages
requires the matching assistant tool_calls message with intact ids, and a window
that starts mid-turn would slice that pairing apart, which Ollama rejects. And
the answers already state the figures the tools returned, so the facts survive;
what is lost is only the model's ability to see *how* it got them.

Budgets, not trust
------------------
A stored message is user-controlled text up to 8000 characters, and an agent
answer can be longer. Ten of those would be a meaningful fraction of the context
window before the question is even asked, so both a per-message clip and a total
character budget apply, oldest dropped first.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Message

log = logging.getLogger(__name__)

__all__ = ["load_history", "to_messages", "WINDOW_MESSAGES"]

# How many prior messages to carry. Ten is five exchanges -- enough that a
# reference can reach back a few turns, small enough to stay cheap on a 7B model
# running on CPU. Raise this and the honest next step is summarization of what
# falls off the end, not just a bigger number: at some thread length the window
# stops fitting, and silently dropping the oldest turns is how a conversation
# starts contradicting itself.
WINDOW_MESSAGES = 10

# Roles the model can be shown. The table's CHECK also permits "system" and
# "tool": a stored system message is this application's prompt, which the
# orchestrator writes itself and must not receive a second, older copy of, and a
# stored tool message cannot be replayed without its assistant pair (see above).
_REPLAYABLE_ROLES = ("user", "assistant")

# Per-message clip. Long enough to hold a full agent answer with its figures,
# short enough that one message cannot eat the window.
MAX_MESSAGE_CHARS = 2000

# Total across the window. At ~4 chars per token this is roughly 2k tokens of
# history, leaving the system prompt, four tool schemas and the tool results
# room in a 32k context.
MAX_TOTAL_CHARS = 8000

_CLIP_NOTE = " …[truncated]"


def _clip(content: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Shorten a message, marking that it was shortened.

    The marker matters: an answer cut off mid-sentence with no sign of it reads
    to the model as an answer that trailed off, and it will sometimes try to
    finish the thought instead of answering the new question.
    """
    if len(content) <= limit:
        return content
    return content[: limit - len(_CLIP_NOTE)].rstrip() + _CLIP_NOTE


async def load_history(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    limit: int = WINDOW_MESSAGES,
    before_id: int | None = None,
) -> list[dict[str, str]]:
    """The last `limit` replayable messages of a conversation, oldest first.

    Args:
        session: the request's session. Reused rather than opening a second
            connection, so the read sees this request's transaction.
        conversation_id: assumed already authorized by the caller -- this does
            not check ownership, and must not be called with an id that has not
            been through `owned_conversation`.
        limit: window size. 0 or negative returns nothing, which is how a caller
            asks for the single-turn behaviour explicitly.
        before_id: exclude messages with an id at or above this. Not needed by
            /ask, which loads history before writing the current turn, but makes
            the function correct for a caller that writes first.

    Returns:
        `[{"role": ..., "content": ...}]` in chronological order, ready to hand
        to `run_agent(history=...)`. Empty for a new conversation.
    """
    if limit <= 0:
        return []

    stmt = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.role.in_(_REPLAYABLE_ROLES),
    )
    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)

    # Newest-first with a LIMIT, then reversed. Ordering ascending and taking
    # the last N would make Postgres read the whole thread to discard most of
    # it; this walks the (conversation_id, id) index backwards and stops at N.
    stmt = stmt.order_by(Message.id.desc()).limit(limit)

    rows = list(await session.scalars(stmt))
    rows.reverse()
    return to_messages(rows)


def to_messages(rows: list[Any]) -> list[dict[str, str]]:
    """Turn stored rows into chat messages, within the character budget.

    Trimming drops from the *front*: the turn immediately before the question is
    what a follow-up refers to, so if something has to go, it is the oldest.
    Separated from the query so it can be tested on plain objects without a
    database.
    """
    messages = [
        {"role": row.role, "content": _clip(row.content or "")}
        for row in rows
        if row.role in _REPLAYABLE_ROLES and (row.content or "").strip()
    ]

    total = sum(len(m["content"]) for m in messages)
    dropped = 0
    while messages and total > MAX_TOTAL_CHARS:
        total -= len(messages[0]["content"])
        messages.pop(0)
        dropped += 1

    if dropped:
        log.info("memory: dropped %d oldest message(s) to fit the character budget",
                 dropped)
    return messages
