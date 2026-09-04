"""Cross-user access control: user A must never reach user B's data.

Runs against a live Postgres (DATABASE_URL, migrated to head). Skipped with a
clear message if the database is unreachable, so it never passes silently.

Rate limiting is switched off for this module. It is IP-keyed for /auth/*, and
every request here arrives from the same TestClient address, so leaving it on
would make repeated runs fail on the limiter rather than on what is being
tested. Rate limiting has its own suite in tests/test_rate_limit.py.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core.config import settings

PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="module", autouse=True)
def no_rate_limit():
    original = settings.rate_limit_enabled
    settings.rate_limit_enabled = False
    yield
    settings.rate_limit_enabled = original


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        health = c.get("/health/db")
        if health.status_code != 200:
            pytest.skip(f"Postgres unreachable ({health.text}); run migrations first")
        yield c


def make_user(client: TestClient) -> dict:
    """Sign up a fresh user and return their identity plus auth header."""
    email = f"iso-{uuid.uuid4().hex[:12]}@example.com"
    response = client.post("/auth/signup", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return {"email": email, "headers": headers, "id": me.json()["id"]}


@pytest.fixture
def alice_and_bob(client):
    """Two users, each with one conversation holding one private message."""
    users = {}
    for name, secret in (("alice", "ALICE-PRIVATE-NOTE"), ("bob", "BOB-PRIVATE-NOTE")):
        user = make_user(client)

        conversation = client.post("/conversations", json={}, headers=user["headers"])
        assert conversation.status_code == 201, conversation.text
        user["conversation_id"] = conversation.json()["id"]

        message = client.post(
            f"/conversations/{user['conversation_id']}/messages",
            json={"role": "user", "content": secret},
            headers=user["headers"],
        )
        assert message.status_code == 201, message.text
        user["secret"] = secret
        user["message_id"] = message.json()["id"]

        users[name] = user

    assert users["alice"]["id"] != users["bob"]["id"]
    assert users["alice"]["conversation_id"] != users["bob"]["conversation_id"]
    return users


# ── the core question ────────────────────────────────────────────────────────

def test_read_another_users_history_is_denied(client, alice_and_bob):
    alice, bob = alice_and_bob["alice"], alice_and_bob["bob"]

    response = client.get(
        f"/conversations/{bob['conversation_id']}/messages", headers=alice["headers"]
    )

    assert response.status_code == 404, response.text
    # 404 and not 403: a 403 would confirm the conversation exists, which is an
    # existence oracle for another user's data.
    assert response.status_code != 403
    assert bob["secret"] not in response.text
    assert "BOB-PRIVATE-NOTE" not in response.text


def test_write_into_another_users_conversation_is_denied(client, alice_and_bob):
    alice, bob = alice_and_bob["alice"], alice_and_bob["bob"]

    response = client.post(
        f"/conversations/{bob['conversation_id']}/messages",
        json={"role": "user", "content": "ALICE WAS HERE"},
        headers=alice["headers"],
    )
    assert response.status_code == 404, response.text

    # And Bob's conversation is genuinely unchanged, not merely un-reported.
    bobs_view = client.get(
        f"/conversations/{bob['conversation_id']}/messages", headers=bob["headers"]
    )
    assert bobs_view.status_code == 200
    contents = [m["content"] for m in bobs_view.json()["messages"]]
    assert contents == [bob["secret"]]
    assert "ALICE WAS HERE" not in bobs_view.text


def test_conversation_list_is_scoped_to_the_caller(client, alice_and_bob):
    alice, bob = alice_and_bob["alice"], alice_and_bob["bob"]

    alices_list = client.get("/conversations", headers=alice["headers"])
    assert alices_list.status_code == 200
    ids = {c["id"] for c in alices_list.json()}

    assert alice["conversation_id"] in ids
    assert bob["conversation_id"] not in ids


def test_unknown_and_forbidden_are_indistinguishable(client, alice_and_bob):
    """A conversation that does not exist and one Alice may not see look identical."""
    alice, bob = alice_and_bob["alice"], alice_and_bob["bob"]

    nonexistent = client.get(f"/conversations/{uuid.uuid4()}/messages", headers=alice["headers"])
    forbidden = client.get(
        f"/conversations/{bob['conversation_id']}/messages", headers=alice["headers"]
    )

    assert nonexistent.status_code == forbidden.status_code == 404
    assert nonexistent.json() == forbidden.json()


def test_no_token_and_forged_token_are_rejected(client, alice_and_bob):
    bob = alice_and_bob["bob"]
    path = f"/conversations/{bob['conversation_id']}/messages"

    assert client.get(path).status_code == 401

    # A token signed with the wrong key must not be accepted.
    from jose import jwt

    forged = jwt.encode(
        {"sub": bob["id"], "typ": "access", "exp": 9999999999}, "not-the-real-secret", algorithm="HS256"
    )
    response = client.get(path, headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401, response.text
    assert bob["secret"] not in response.text
