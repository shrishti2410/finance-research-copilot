"""Tests for POST /ask.

The agent loop is replaced with a stub, so these cover what the endpoint owns:
access control, that both messages are stored, and that a run which did not
complete is still reported honestly. Runs against the live Postgres in
DATABASE_URL and skips when it is unreachable.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from agent.orchestrator import AgentResult, Step
from api import routes
from api.main import app
from core.config import settings

PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="module", autouse=True)
def no_rate_limit():
    # IP-keyed for /auth/*, and every request here shares the TestClient's
    # address, so repeated runs would fail on the limiter instead of on the
    # thing under test. Rate limiting has its own suite.
    original = settings.rate_limit_enabled
    settings.rate_limit_enabled = False
    yield
    settings.rate_limit_enabled = original


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        if c.get("/health/db").status_code != 200:
            pytest.skip("Postgres unreachable; run migrations first")
        yield c


def make_user(client: TestClient) -> dict:
    email = f"ask-{uuid.uuid4().hex[:12]}@example.com"
    client.post("/auth/signup", json={"email": email, "password": PASSWORD})
    token = client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def make_conversation(client: TestClient, headers: dict) -> str:
    return client.post("/conversations", json={"title": None}, headers=headers).json()["id"]


ANSWER = "NVIDIA's gross margin is 71.07%; Apple's is 46.9%."


def fake_result(**overrides) -> AgentResult:
    defaults = dict(
        answer=ANSWER,
        steps=[
            Step(1, "calculate_ratio", {"ticker": "NVDA", "ratio_name": "gross_margin"},
                 {"ok": True, "value": 0.7107}, 120.0, 4000.0, True),
            Step(2, "final_answer", {}, ANSWER, 3000.0, 3000.0, True),
        ],
        iterations=2, completed=True, stop_reason="final_answer",
        model="qwen2.5:7b", total_ms=7120.0,
    )
    return AgentResult(**{**defaults, **overrides})


@pytest.fixture
def stub_agent(monkeypatch):
    """Replace the loop. Returns a knob for what it should produce."""
    state = {"result": fake_result(), "calls": []}

    async def fake_run_agent(question, history=None, **kwargs):
        state["calls"].append({"question": question, **kwargs})
        return state["result"]

    monkeypatch.setattr(routes, "run_agent", fake_run_agent)
    return state


# ── access control ───────────────────────────────────────────────────────────

def test_requires_authentication(client):
    response = client.post("/ask", json={
        "conversation_id": str(uuid.uuid4()), "message": "hi"
    })
    assert response.status_code == 401


def test_another_users_conversation_is_404_not_403(client, stub_agent):
    """A 403 confirms the id exists, which turns this into an existence oracle
    for other people's threads."""
    owner = make_user(client)
    conversation_id = make_conversation(client, owner)

    intruder = make_user(client)
    response = client.post(
        "/ask", json={"conversation_id": conversation_id, "message": "hi"}, headers=intruder
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_the_intruders_message_is_not_stored(client, stub_agent):
    owner = make_user(client)
    conversation_id = make_conversation(client, owner)
    intruder = make_user(client)

    client.post("/ask", json={"conversation_id": conversation_id, "message": "leak"},
                headers=intruder)

    history = client.get(f"/conversations/{conversation_id}/messages", headers=owner).json()
    assert history["messages"] == []


def test_an_unknown_conversation_is_404(client, stub_agent):
    headers = make_user(client)
    response = client.post(
        "/ask", json={"conversation_id": str(uuid.uuid4()), "message": "hi"}, headers=headers
    )
    assert response.status_code == 404


# ── storage ──────────────────────────────────────────────────────────────────

def test_both_sides_of_the_turn_are_stored(client, stub_agent):
    headers = make_user(client)
    conversation_id = make_conversation(client, headers)

    response = client.post(
        "/ask", json={"conversation_id": conversation_id, "message": "Compare margins"},
        headers=headers,
    )
    assert response.status_code == 200

    messages = client.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    ).json()["messages"]

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Compare margins"
    assert messages[1]["content"] == ANSWER


def test_the_response_points_at_the_stored_messages(client, stub_agent):
    headers = make_user(client)
    conversation_id = make_conversation(client, headers)

    body = client.post(
        "/ask", json={"conversation_id": conversation_id, "message": "q"}, headers=headers
    ).json()
    stored = client.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    ).json()["messages"]

    assert body["user_message_id"] == stored[0]["id"]
    assert body["assistant_message_id"] == stored[1]["id"]


def test_the_trace_is_stored_on_the_assistant_message(client, stub_agent):
    """The audit trail lives with the answer, so it survives the response."""
    headers = make_user(client)
    conversation_id = make_conversation(client, headers)
    client.post("/ask", json={"conversation_id": conversation_id, "message": "q"},
                headers=headers)

    assistant = client.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    ).json()["messages"][1]

    assert assistant["meta"]["agent"]["model"] == "qwen2.5:7b"
    assert assistant["meta"]["agent"]["tools_called"] == ["calculate_ratio"]
    assert assistant["meta"]["trace"][0]["tool"] == "calculate_ratio"


def test_the_conversation_title_comes_from_the_question(client, stub_agent):
    headers = make_user(client)
    conversation_id = make_conversation(client, headers)
    client.post("/ask", json={"conversation_id": conversation_id, "message": "Compare margins"},
                headers=headers)

    threads = client.get("/conversations", headers=headers).json()
    assert next(t for t in threads if t["id"] == conversation_id)["title"] == "Compare margins"


def test_nothing_is_stored_when_the_agent_raises(client, monkeypatch):
    """Both messages commit together. A user message persisted without the
    answer is a thread that looks unanswered."""
    headers = make_user(client)
    conversation_id = make_conversation(client, headers)

    async def explode(*args, **kwargs):
        raise RuntimeError("loop blew up")

    monkeypatch.setattr(routes, "run_agent", explode)
    with pytest.raises(RuntimeError):
        client.post("/ask", json={"conversation_id": conversation_id, "message": "q"},
                    headers=headers)

    messages = client.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    ).json()["messages"]
    assert messages == []


# ── reporting an incomplete run ──────────────────────────────────────────────

def test_an_exhausted_run_is_reported_as_incomplete(client, stub_agent):
    """The answer field is never empty, so `completed` is the only way a caller
    can tell a real answer from a loop that ran out."""
    stub_agent["result"] = fake_result(
        answer="I couldn't complete this question within the 5-step limit.",
        completed=False, stop_reason="max_iterations", iterations=5,
    )
    headers = make_user(client)
    conversation_id = make_conversation(client, headers)

    body = client.post(
        "/ask", json={"conversation_id": conversation_id, "message": "q"}, headers=headers
    ).json()

    assert body["completed"] is False
    assert body["stop_reason"] == "max_iterations"
    assert body["answer"]                      # still written, never empty


def test_the_incomplete_answer_is_still_stored(client, stub_agent):
    """The user saw it; the thread must show what they saw."""
    stub_agent["result"] = fake_result(
        answer="I couldn't complete this.", completed=False, stop_reason="max_iterations"
    )
    headers = make_user(client)
    conversation_id = make_conversation(client, headers)
    client.post("/ask", json={"conversation_id": conversation_id, "message": "q"},
                headers=headers)

    messages = client.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    ).json()["messages"]
    assert messages[1]["content"] == "I couldn't complete this."
    assert messages[1]["meta"]["agent"]["completed"] is False


# ── the trace in the response ────────────────────────────────────────────────

def test_the_trace_is_omitted_by_default(client, stub_agent):
    headers = make_user(client)
    conversation_id = make_conversation(client, headers)
    body = client.post(
        "/ask", json={"conversation_id": conversation_id, "message": "q"}, headers=headers
    ).json()
    assert body["steps"] is None


def test_the_trace_is_returned_when_asked_for(client, stub_agent):
    headers = make_user(client)
    conversation_id = make_conversation(client, headers)
    body = client.post(
        "/ask",
        json={"conversation_id": conversation_id, "message": "q", "include_trace": True},
        headers=headers,
    ).json()

    assert [step["tool"] for step in body["steps"]] == ["calculate_ratio", "final_answer"]
    assert body["steps"][0]["latency_ms"] == 120.0


# ── input validation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", ["", "x" * 8001])
def test_message_length_is_validated(client, stub_agent, message):
    headers = make_user(client)
    conversation_id = make_conversation(client, headers)
    response = client.post(
        "/ask", json={"conversation_id": conversation_id, "message": message}, headers=headers
    )
    assert response.status_code == 422


def test_a_malformed_conversation_id_is_422(client, stub_agent):
    headers = make_user(client)
    response = client.post(
        "/ask", json={"conversation_id": "not-a-uuid", "message": "hi"}, headers=headers
    )
    assert response.status_code == 422
