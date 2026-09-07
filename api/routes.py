"""The agent surface.

    POST /ask           ask a question; get the answer when the run finishes
    POST /ask/stream    the same run, reported as Server-Sent Events

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

import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.memory import load_history
from agent.orchestrator import run_agent
from api.routes_chat import append_message, owned_conversation
from api.schemas import AskRequest, AskResponse
from auth.deps import get_current_user
from core.config import settings
from db.base import SessionLocal, get_session
from db.models import Conversation, User

log = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])

# Trace steps kept on the stored assistant message. The whole trace is usually
# small, but a tool result is not bounded -- a filings search carries excerpts.
# The row is the audit trail, not the archive.
MAX_STORED_STEPS = 40


async def _store_turn(session, conversation, question, result, history_messages=0):
    """Persist both sides of one turn, in one transaction.

    Shared by /ask and /ask/stream so the row a streamed answer leaves behind is
    byte-identical to a buffered one -- a client must not be able to tell which
    endpoint produced a message by reading it back.

    The question and the answer commit together, after the run. Writing the
    question first would leave, on an agent failure, a thread whose last message
    is a question nobody answered -- indistinguishable on reload from a request
    still in flight.
    """
    user_message = append_message(session, conversation, "user", question)
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
                "history_messages": history_messages,
            },
            "trace": [step.to_dict() for step in result.steps[:MAX_STORED_STEPS]],
        },
    )

    await session.commit()
    await session.refresh(user_message)
    await session.refresh(assistant_message)
    return user_message, assistant_message


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

    user_message, assistant_message = await _store_turn(
        session, conversation, body.message, result, history_messages=len(history)
    )

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


# ─────────────────────────────────────────────────────────────────────────────
# POST /ask/stream
# ─────────────────────────────────────────────────────────────────────────────
#
# Same run as /ask, reported as it happens. A five-iteration loop on CPU is tens
# of seconds; a browser given no signal for that long is indistinguishable from
# one that is broken, and the interesting part -- which tool is running, and why
# -- is over before the buffered endpoint says anything at all.
#
# Server-Sent Events, not WebSockets: this is one-way, short-lived, and survives
# a proxy that only understands HTTP. Delivered over POST rather than an
# EventSource, because EventSource cannot set an Authorization header; the
# browser reads it with fetch() and a stream reader.
#
# Persistence is unchanged. Both messages still commit together, after the run,
# in one transaction -- streaming changes what the client *sees* during the run,
# not what the database holds if it fails. The ids arrive in the final "done"
# event, which is the client's signal that the answer on screen is now a row.

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Without this a buffering reverse proxy collects the whole stream and
    # delivers it in one lump, which looks exactly like streaming being broken.
    "X-Accel-Buffering": "no",
}


def sse(event: dict) -> str:
    """One SSE frame. `json.dumps` guarantees the payload has no bare newline,
    which would otherwise terminate the frame early."""
    return f"data: {json.dumps(event, default=str)}\n\n"


# Runs that outlived their request. Referenced here so the event loop cannot
# collect them mid-flight; see where they are added.
_RUNS: set[asyncio.Task] = set()


async def _run_and_store(*, conversation_id, user_id, question, history, queue, put):
    """Run the agent and persist the turn, on a session of its own.

    The request's session is deliberately not used. It is closed when the
    response ends, and the whole point of this task is to still be running then
    -- writing through it after the client disconnects fails on a closed
    connection, which is how a finished answer gets silently dropped.

    The conversation is re-fetched here rather than passed in: an ORM object
    belongs to the session that loaded it, and `append_message` mutates it.
    Ownership was already established in the request, so this reads by id --
    and still scopes by user_id, so a stale task can never write into a
    conversation that changed hands.
    """
    try:
        async with SessionLocal() as session:
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                )
            )
            if conversation is None:  # deleted between the check and now
                await queue.put({
                    "type": "error",
                    "error": "NotFound",
                    "message": "The conversation no longer exists.",
                })
                return

            result = await run_agent(
                question,
                history=history,
                max_iterations=settings.agent_max_iterations,
                max_tool_calls_per_iteration=settings.agent_max_tool_calls_per_iteration,
                stream_tokens=True,
                on_token=lambda text: put({"type": "token", "text": text}),
                on_event=put,
            )
            user_message, assistant_message = await _store_turn(
                session, conversation, question, result,
                history_messages=len(history),
            )
            await queue.put({
                "type": "done",
                "conversation_id": str(conversation_id),
                "user_message_id": user_message.id,
                "assistant_message_id": assistant_message.id,
                "answer": result.answer,
                "completed": result.completed,
                "stop_reason": result.stop_reason,
                "iterations": result.iterations,
                "model": result.model,
                "total_ms": round(result.total_ms, 1),
                "history_messages": len(history),
                "steps": [step.to_dict() for step in result.steps],
            })
    except Exception as exc:  # noqa: BLE001 - the client is owed a reason
        log.exception("ask/stream: run failed for conversation %s", conversation_id)
        await queue.put({
            "type": "error",
            "error": type(exc).__name__,
            "message": str(exc),
        })
    finally:
        await queue.put(None)  # sentinel: nothing more is coming


@router.post("/ask/stream")
async def ask_stream(
    body: AskRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Run a question through the agent loop, reporting progress as it happens.

    Emits, in order:
      - `iteration`   a model round-trip started
      - `tool_start`  the tools this iteration asked for, before they run
      - `tool_result` one finished tool call, with its arguments and outcome
      - `token`       a fragment of the answer as it is written
      - `discarded`   the grounding guard rejected a draft; reset any shown text
      - `budget`      the per-iteration tool-call budget stopped the run
      - `done`        the stored message ids, the final answer, and the trace
      - `error`       the run failed; nothing was stored

    The access check happens before the response starts, so an unauthorized
    caller still gets a real 404 rather than a 200 whose first frame is bad news.
    """
    conversation = await owned_conversation(body.conversation_id, user, session)
    history = await load_history(
        session, conversation.id, limit=settings.agent_history_messages
    )

    # A queue decouples the loop from the socket, so a slow reader cannot
    # throttle tool execution and a vanished one cannot stall it.
    queue: asyncio.Queue = asyncio.Queue()

    async def put(event: dict) -> None:
        await queue.put(event)

    task = asyncio.create_task(
        _run_and_store(
            conversation_id=conversation.id,
            user_id=user.id,
            question=body.message,
            history=history,
            queue=queue,
            put=put,
        )
    )
    # A task referenced only by the event loop can be garbage-collected
    # mid-flight. Holding it until it finishes is what makes "the run survives
    # the client" true rather than merely likely.
    _RUNS.add(task)
    task.add_done_callback(_RUNS.discard)

    async def frames():
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield sse(event)
        finally:
            # Deliberately not cancelled. A client hanging up -- a refresh, a
            # closed tab -- is not a reason to throw away a run that has already
            # spent a minute of model time and made real tool calls. It owns its
            # own database session precisely so it can outlive this request and
            # still commit; the reloaded page then finds the finished turn
            # waiting for it.
            if not task.done():
                log.info(
                    "ask/stream: client left; conversation %s continues in the "
                    "background",
                    conversation.id,
                )

    return StreamingResponse(
        frames(), media_type="text/event-stream", headers=SSE_HEADERS
    )
