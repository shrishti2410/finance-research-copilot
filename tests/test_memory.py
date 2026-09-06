"""Tests for agent/memory.py.

`to_messages` is tested on plain objects -- it is pure, and a database adds
nothing to it. `load_history` needs real rows, so those tests run against the
Postgres in DATABASE_URL and skip when it is unreachable, matching the rest of
the DB-backed suites.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest

from agent.memory import (
    MAX_MESSAGE_CHARS,
    MAX_TOTAL_CHARS,
    WINDOW_MESSAGES,
    load_history,
    to_messages,
)


@dataclass
class Row:
    """Stands in for db.models.Message: memory only reads role and content."""

    role: str
    content: str


# ─────────────────────────────────────────────────────────────────────────────
# to_messages
# ─────────────────────────────────────────────────────────────────────────────

def test_rows_become_chat_messages_in_order():
    rows = [
        Row("user", "What is NVIDIA's gross margin?"),
        Row("assistant", "About 71%."),
        Row("user", "What about Apple's?"),
    ]
    assert to_messages(rows) == [
        {"role": "user", "content": "What is NVIDIA's gross margin?"},
        {"role": "assistant", "content": "About 71%."},
        {"role": "user", "content": "What about Apple's?"},
    ]


def test_tool_and_system_rows_are_not_replayed():
    """A stored tool message has no assistant tool_calls message to pair with
    once it is sliced into a window, and Ollama rejects the orphan. A stored
    system message would be a second, older copy of the prompt."""
    rows = [
        Row("system", "You are a helpful assistant."),
        Row("user", "Question?"),
        Row("tool", '{"ok": true, "value": 0.71}'),
        Row("assistant", "Answer."),
    ]
    assert [m["role"] for m in to_messages(rows)] == ["user", "assistant"]


def test_empty_and_whitespace_messages_are_skipped():
    rows = [Row("user", "real"), Row("assistant", "   "), Row("user", "")]
    assert to_messages(rows) == [{"role": "user", "content": "real"}]


def test_no_rows_is_an_empty_history_not_an_error():
    assert to_messages([]) == []


def test_a_long_message_is_clipped_and_says_so():
    rows = [Row("assistant", "x" * (MAX_MESSAGE_CHARS + 500))]
    content = to_messages(rows)[0]["content"]

    assert len(content) <= MAX_MESSAGE_CHARS
    # The marker is load-bearing: a silently truncated answer reads to the model
    # as one that trailed off, and it tries to finish the thought.
    assert content.endswith("…[truncated]")


def test_a_message_at_the_limit_is_untouched():
    exact = "x" * MAX_MESSAGE_CHARS
    assert to_messages([Row("user", exact)])[0]["content"] == exact


def test_the_budget_drops_the_oldest_first():
    """The turn right before the question is what a follow-up refers to, so the
    front of the window is what goes."""
    big = "x" * MAX_MESSAGE_CHARS
    rows = [Row("user", f"{i} {big}") for i in range(10)]

    kept = to_messages(rows)

    assert sum(len(m["content"]) for m in kept) <= MAX_TOTAL_CHARS
    assert kept, "the budget must not empty the window entirely"
    # Whatever survived, the newest message did.
    assert kept[-1]["content"].startswith("9 ")


def test_a_window_inside_the_budget_is_not_trimmed():
    rows = [Row("user", "short"), Row("assistant", "also short")]
    assert len(to_messages(rows)) == 2


# ─────────────────────────────────────────────────────────────────────────────
# load_history (needs Postgres)
# ─────────────────────────────────────────────────────────────────────────────
#
# One event loop for the module, matching tests/test_retrieval.py: db.base
# pools connections bound to the loop that opened them, so a fresh
# `asyncio.run` per test would find a connection whose loop is closed.

_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def close_loop():
    yield
    from db.base import engine
    _LOOP.run_until_complete(engine.dispose())
    _LOOP.close()


@pytest.fixture(scope="module")
def session():
    """One session for the module. Rolled back at the end, so these tests leave
    no user, conversation or message behind in the shared database."""
    from db.base import SessionLocal

    try:
        opened = run(SessionLocal().__aenter__())
    except Exception as exc:  # noqa: BLE001 - Postgres down is a skip, not a failure
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}: {exc}")

    yield opened
    run(opened.rollback())
    run(opened.close())


@pytest.fixture(scope="module")
def seeded(session):
    """A conversation with 12 alternating messages, oldest first."""
    from db.models import Conversation, Message, User

    user = User(
        email=f"memory-{uuid.uuid4().hex[:12]}@example.com",
        password_hash="x" * 60,
    )
    session.add(user)
    run(session.flush())

    conversation = Conversation(user_id=user.id, title="memory test")
    session.add(conversation)
    run(session.flush())

    for i in range(12):
        session.add(Message(
            conversation_id=conversation.id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"message {i}",
        ))
    run(session.flush())
    return conversation


def test_the_window_is_the_last_n_messages_in_order(session, seeded):
    history = run(load_history(session, seeded.id, limit=4))

    assert [m["content"] for m in history] == [
        "message 8", "message 9", "message 10", "message 11",
    ]


def test_the_default_window_is_ten(session, seeded):
    history = run(load_history(session, seeded.id))

    assert len(history) == WINDOW_MESSAGES == 10
    assert history[0]["content"] == "message 2"     # the two oldest fell off
    assert history[-1]["content"] == "message 11"


def test_roles_alternate_as_stored(session, seeded):
    history = run(load_history(session, seeded.id, limit=4))
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]


def test_a_shorter_thread_returns_everything_it_has(session, seeded):
    assert len(run(load_history(session, seeded.id, limit=50))) == 12


def test_a_new_conversation_has_no_history(session, seeded):
    from db.models import Conversation

    fresh = Conversation(user_id=seeded.user_id, title=None)
    session.add(fresh)
    run(session.flush())

    assert run(load_history(session, fresh.id)) == []


def test_a_zero_window_is_how_a_caller_asks_for_single_turn(session, seeded):
    """Explicit, and it must not fall through to the default."""
    assert run(load_history(session, seeded.id, limit=0)) == []
    assert run(load_history(session, seeded.id, limit=-1)) == []


def test_before_id_excludes_the_current_turn(session, seeded):
    """For a caller that writes the question before running the agent."""
    from sqlalchemy import select

    from db.models import Message

    ids = list(run(session.scalars(
        select(Message.id).where(Message.conversation_id == seeded.id)
        .order_by(Message.id)
    )))

    history = run(load_history(session, seeded.id, limit=4, before_id=ids[-1]))

    assert [m["content"] for m in history] == [
        "message 7", "message 8", "message 9", "message 10",
    ]


def test_history_is_scoped_to_one_conversation(session, seeded):
    """Two threads owned by the same user must not bleed into each other."""
    from db.models import Conversation, Message

    other = Conversation(user_id=seeded.user_id, title="other thread")
    session.add(other)
    run(session.flush())
    session.add(Message(
        conversation_id=other.id, role="user", content="unrelated question"
    ))
    run(session.flush())

    assert [m["content"] for m in run(load_history(session, other.id))] == [
        "unrelated question"
    ]


def test_stored_tool_rows_are_left_out_of_the_window(session, seeded):
    """The window is sliced by LIMIT in SQL, so excluding tool rows has to
    happen in the query -- filtering afterwards would return three rows and
    silently hand back two."""
    from db.models import Message

    session.add(Message(
        conversation_id=seeded.id, role="tool", content='{"ok": true}'
    ))
    run(session.flush())

    history = run(load_history(session, seeded.id, limit=3))

    assert [m["role"] for m in history] == ["assistant", "user", "assistant"]
    assert history[-1]["content"] == "message 11"
