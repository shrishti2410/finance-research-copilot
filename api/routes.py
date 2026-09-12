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

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from agent.memory import load_history
from agent.orchestrator import run_agent
from api.routes_chat import append_message, owned_conversation
from api.schemas import AskRequest, AskResponse
from auth.deps import get_current_user
from core.config import settings
from db.base import ConnectionPoolExhausted, session_scope
from db.models import Conversation, User

log = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])

# Trace steps kept on the stored assistant message. The whole trace is usually
# small, but a tool result is not bounded -- a filings search carries excerpts.
# The row is the audit trail, not the archive.
MAX_STORED_STEPS = 40


async def _store_turn_in_new_session(
    conversation_id, user_id, question, result, *, history_messages=0,
    attempts: int = 4,
):
    """Open a session, re-read the conversation, write the turn, release.

    Phase 3 of the split described on `ask`. Two things need care here that did
    not when one session spanned the whole request.

    **The conversation is re-read, scoped by user_id.** An ORM object belongs to
    the session that loaded it and `append_message` mutates it, so the phase-1
    instance cannot be reused. Re-reading also means a conversation deleted during
    the run is noticed instead of being written to. Keeping `user_id` in the
    predicate matters: ownership was established minutes ago, and a conversation
    that changed hands since must not receive this answer.

    **The write retries on pool exhaustion.** Everywhere else a full pool should
    fail fast and let the caller retry, but by this point the answer has cost
    minutes of inference and cannot be recreated -- dropping it to save a 30-second
    wait is the wrong trade. The writes themselves are milliseconds, so a
    connection frees up quickly even under load. A read failing in phase 1 costs
    the user a retry; a write failing here costs them the answer.
    """
    delay = 0.5
    for attempt in range(1, attempts + 1):
        try:
            async with session_scope() as session:
                conversation = await session.scalar(
                    select(Conversation).where(
                        Conversation.id == conversation_id,
                        Conversation.user_id == user_id,
                    )
                )
                if conversation is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="The conversation no longer exists.",
                    )
                return await _store_turn(
                    session, conversation, question, result,
                    history_messages=history_messages,
                )
        except ConnectionPoolExhausted:
            if attempt == attempts:
                # Out of attempts. The answer is lost, and saying so plainly beats
                # a 503 that implies nothing happened -- the run did happen, and
                # it was expensive.
                log.error(
                    "ask: could not store a completed turn for conversation %s "
                    "after %d attempts; the answer is lost",
                    conversation_id, attempts,
                )
                raise
            log.warning(
                "ask: pool full while storing conversation %s, retrying in %.1fs "
                "[attempt %d of %d]",
                conversation_id, delay, attempt, attempts,
            )
            await asyncio.sleep(delay)
            delay *= 2


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
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "usage_measured": result.usage_measured,
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
) -> AskResponse:
    """Run a question through the agent loop and store both sides of the turn.

    404s for a conversation the caller does not own, matching the rest of the
    conversation routes: a 403 would confirm the id exists and turn this into an
    existence oracle for other people's threads.

    ── Three phases, and a connection held for only two of them ────────────────

    Deliberately no `Depends(get_session)`. A dependency's session is released
    when the response completes, which here is after the agent has finished --
    minutes during which the connection sat idle waiting on the model. With
    `db_pool_size + db_max_overflow` at 15, that made 15 the concurrency ceiling
    of the deployment regardless of hardware; the load test hit it at ten users.

    So the database is used for two short bursts with the long part in between
    holding nothing:

        1. read    ownership check and the history window       (milliseconds)
        2. think   inference and tool calls, no connection      (minutes)
        3. write   both messages, one transaction               (milliseconds)

    The cost of the split is that the conversation must be re-read in phase 3,
    and can have been deleted in the meantime. That is handled explicitly below
    and is strictly more honest than the previous arrangement, which held a
    transaction open across the whole run and could not have noticed.
    """
    async with session_scope() as session:
        conversation = await owned_conversation(body.conversation_id, user, session)
        conversation_id = conversation.id
        # Read before writing this turn, so the window is the prior conversation
        # and the current question appears exactly once -- run_agent appends it.
        history = await load_history(
            session, conversation_id, limit=settings.agent_history_messages
        )

    # ── no database connection is held from here until the write ─────────────
    result = await run_agent(
        body.message,
        history=history,
        max_iterations=settings.agent_max_iterations,
        max_tool_calls_per_iteration=settings.agent_max_tool_calls_per_iteration,
    )

    log.info(
        "ask: conversation=%s user=%s history=%d iterations=%d completed=%s "
        "stop=%s tools=%s",
        conversation_id, user.id, len(history), result.iterations, result.completed,
        result.stop_reason, [step.tool for step in result.tool_calls],
    )

    user_message, assistant_message = await _store_turn_in_new_session(
        conversation_id, user.id, body.message, result, history_messages=len(history)
    )

    return AskResponse(
        conversation_id=conversation_id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message.id,
        answer=result.answer,
        completed=result.completed,
        stop_reason=result.stop_reason,
        iterations=result.iterations,
        model=result.model,
        total_ms=round(result.total_ms, 1),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        usage_measured=result.usage_measured,
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
    """Run the agent with no connection held, then persist the turn.

    The request's session is deliberately not used, and neither is one of this
    task's own for the duration. This task outlives its request by design, so a
    session borrowed from the request would be closed underneath it -- and a
    session it opened itself would pin a pooled connection for the whole run,
    which is what made a streaming request cost **two** connections and put the
    ceiling at ten concurrent users rather than fifteen.

    So: run first, connect afterwards. The conversation is re-read inside the
    write, scoped by user_id, for the reasons in
    `_store_turn_in_new_session`.

    One behaviour change worth naming: the conversation used to be re-read
    *before* the run, so a deletion during the request was caught early and the
    agent never ran. Now the run happens first and the deletion is noticed at the
    write. That wastes an inference on a conversation nobody can read, which is
    cheap and rare, and it buys back the connection that the early check was
    holding for minutes.
    """
    try:
        result = await run_agent(
            question,
            history=history,
            max_iterations=settings.agent_max_iterations,
            max_tool_calls_per_iteration=settings.agent_max_tool_calls_per_iteration,
            stream_tokens=True,
            on_token=lambda text: put({"type": "token", "text": text}),
            on_event=put,
        )

        try:
            user_message, assistant_message = await _store_turn_in_new_session(
                conversation_id, user_id, question, result,
                history_messages=len(history),
            )
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                await queue.put({
                    "type": "error",
                    "error": "NotFound",
                    "message": "The conversation no longer exists.",
                })
                return
            raise

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

    No `Depends(get_session)`: a dependency's session is released when the
    response completes, and for a stream that is when the last token has been
    sent. Combined with the task's own session that made two pooled connections
    per in-flight question. Both are now short-lived -- one here to read, one in
    `_store_turn_in_new_session` to write -- with none held across the run.
    """
    async with session_scope() as session:
        conversation = await owned_conversation(body.conversation_id, user, session)
        conversation_id = conversation.id
        history = await load_history(
            session, conversation_id, limit=settings.agent_history_messages
        )
    # From here on the request holds no connection.

    # A queue decouples the loop from the socket, so a slow reader cannot
    # throttle tool execution and a vanished one cannot stall it.
    queue: asyncio.Queue = asyncio.Queue()

    async def put(event: dict) -> None:
        await queue.put(event)

    task = asyncio.create_task(
        _run_and_store(
            conversation_id=conversation_id,
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
            # spent a minute of model time and made real tool calls. It opens its own
            # short-lived session only when it has something to write, so it can
            # outlive this request and still commit; the reloaded page then finds the finished turn
            # waiting for it.
            #
            # The cost is that repeated refreshes start repeated runs, each of
            # which finishes and stores a turn. That trade, and what in-flight
            # deduplication would take, is in docs/KNOWN_LIMITATIONS.md.
            if not task.done():
                log.info(
                    "ask/stream: client left; conversation %s continues in the "
                    "background",
                    conversation_id,
                )

    return StreamingResponse(
        frames(), media_type="text/event-stream", headers=SSE_HEADERS
    )
