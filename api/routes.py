"""The agent surface.

    POST /ask    ask a question inside a conversation; get a grounded answer

Persistence reuses `api/routes_chat.py` -- `owned_conversation` for the access
check and `append_message` for the write -- rather than reimplementing either.
The access check in particular is the one that must not be re-derived: there is
exactly one function in this codebase that decides whether a caller may touch a
conversation, and a second copy is a second thing to get wrong.

Both messages are written in one transaction
--------------------------------------------
The user's question and the assistant's answer commit together, after the agent
run. Writing the question first would mean an agent failure leaves a thread
whose last message is a question nobody answered -- indistinguishable, on
reload, from a request still in flight.

The trade is that a client watching the thread sees nothing until the run
finishes, which for a five-iteration loop on CPU is tens of seconds. That is the
right trade for a non-streaming endpoint; a streaming /ask would need the other
one, plus a way to mark a message in progress.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent.memory import load_history
from agent.orchestrator import run_agent
from api.routes_chat import append_message, owned_conversation
from api.schemas import AskRequest, AskResponse
from auth.deps import get_current_user
from core.config import settings
from db.base import get_session
from db.models import User

log = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])

# Trace steps kept on the stored assistant message. The whole trace is usually
# small, but a tool result is not bounded -- a filings search carries excerpts.
# The row is the audit trail, not the archive.
MAX_STORED_STEPS = 40


@router.post("/ask", response_model=AskResponse)
async def ask(
    body: AskRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AskResponse:
    """Run a question through the agent loop and store both sides of the turn.

    404s for a conversation the caller does not own, matching the rest of the
    conversation routes: a 403 would confirm the id exists and turn this into an
    existence oracle for other people's threads.
    """
    conversation = await owned_conversation(body.conversation_id, user, session)

    # Read before writing this turn, so the window is the prior conversation and
    # the current question appears exactly once -- run_agent appends it itself.
    history = await load_history(
        session, conversation.id, limit=settings.agent_history_messages
    )

    result = await run_agent(
        body.message,
        history=history,
        max_iterations=settings.agent_max_iterations,
        max_tool_calls_per_iteration=settings.agent_max_tool_calls_per_iteration,
    )

    log.info(
        "ask: conversation=%s user=%s history=%d iterations=%d completed=%s "
        "stop=%s tools=%s",
        conversation.id, user.id, len(history), result.iterations, result.completed,
        result.stop_reason, [step.tool for step in result.tool_calls],
    )

    user_message = append_message(session, conversation, "user", body.message)
    assistant_message = append_message(
        session, conversation, "assistant", result.answer,
        meta={
            # Everything needed to explain this answer later: which model, how
            # many turns, why it stopped, and what it called to get there.
            "agent": {
                "model": result.model,
                "iterations": result.iterations,
                "completed": result.completed,
                "stop_reason": result.stop_reason,
                "total_ms": round(result.total_ms, 1),
                "tools_called": [step.tool for step in result.tool_calls],
                # How much prior conversation this answer could see. Without it
                # a reference-resolving answer is unexplainable after the fact.
                "history_messages": len(history),
            },
            "trace": [step.to_dict() for step in result.steps[:MAX_STORED_STEPS]],
        },
    )

    await session.commit()
    await session.refresh(user_message)
    await session.refresh(assistant_message)

    return AskResponse(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message.id,
        answer=result.answer,
        completed=result.completed,
        stop_reason=result.stop_reason,
        iterations=result.iterations,
        model=result.model,
        total_ms=round(result.total_ms, 1),
        steps=[step.to_dict() for step in result.steps] if body.include_trace else None,
    )
