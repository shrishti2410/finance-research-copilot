"""End-to-end check of the auth + conversation API against a live Postgres.

Exercises the whole path a client takes:

    signup -> login -> create conversation -> post messages -> read history
    plus the negative cases: duplicate email, wrong password, no token,
    another user's conversation, and keyset pagination.

    python scripts/db_smoke_test.py                     # against DATABASE_URL
    python scripts/db_smoke_test.py --base-url http://127.0.0.1:8000

With no --base-url it drives the app in-process through TestClient, so no server
needs to be running -- but Postgres does, with migrations applied. Non-zero exit
on any failure, so CI can gate on it.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}{('  -- ' + detail) if detail else ''}")


def run(client) -> None:
    email = f"smoke-{uuid.uuid4().hex[:12]}@example.com"
    other_email = f"smoke-{uuid.uuid4().hex[:12]}@example.com"
    password = "correct-horse-battery"

    print("\nauth")
    r = client.post("/auth/signup", json={"email": email, "password": password})
    check("signup returns 201", r.status_code == 201, r.text)
    token = r.json().get("access_token", "")
    check("signup returns a token", bool(token))

    r = client.post("/auth/signup", json={"email": email, "password": password})
    check("duplicate email is 409", r.status_code == 409, r.text)

    r = client.post("/auth/signup", json={"email": "not-an-email", "password": password})
    check("malformed email is 422", r.status_code == 422)

    r = client.post("/auth/signup", json={"email": f"x{email}", "password": "short"})
    check("short password is 422", r.status_code == 422)

    r = client.post("/auth/login", json={"email": email, "password": "wrong"})
    check("wrong password is 401", r.status_code == 401)

    r = client.post("/auth/login", json={"email": email, "password": password})
    check("login returns 200", r.status_code == 200, r.text)
    token = r.json()["access_token"]

    auth = {"Authorization": f"Bearer {token}"}
    r = client.get("/auth/me", headers=auth)
    check("/auth/me returns the caller", r.status_code == 200 and r.json()["email"] == email, r.text)
    check("/auth/me hides the hash", "password_hash" not in r.json())

    check("no token is 401", client.get("/auth/me").status_code == 401)
    check(
        "garbage token is 401",
        client.get("/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401,
    )

    print("\nconversations")
    r = client.post("/conversations", json={}, headers=auth)
    check("create returns 201", r.status_code == 201, r.text)
    conv = r.json()
    conv_id = conv["id"]
    check("new conversation has no title", conv["title"] is None)

    check("create without a token is 401", client.post("/conversations", json={}).status_code == 401)

    print("\nmessages")
    r = client.post(
        f"/conversations/{conv_id}/messages",
        json={"role": "user", "content": "What was NVDA revenue last quarter?"},
        headers=auth,
    )
    check("post message returns 201", r.status_code == 201, r.text)
    first_id = r.json()["id"]

    r = client.get("/conversations", headers=auth)
    titles = [c["title"] for c in r.json() if c["id"] == conv_id]
    check("title derived from first message", titles and titles[0].startswith("What was NVDA"), str(titles))

    r = client.post(
        f"/conversations/{conv_id}/messages",
        json={
            "role": "assistant",
            "content": "Revenue was ...",
            "meta": {"citations": [{"form": "10-Q", "url": "https://sec.gov/x"}]},
        },
        headers=auth,
    )
    check("assistant message with meta returns 201", r.status_code == 201, r.text)
    check("meta round-trips through JSONB", r.json()["meta"]["citations"][0]["form"] == "10-Q")

    r = client.post(
        f"/conversations/{conv_id}/messages",
        json={"role": "sudo", "content": "x"},
        headers=auth,
    )
    check("invalid role is 422", r.status_code == 422)

    print("\nhistory")
    r = client.get(f"/conversations/{conv_id}/messages", headers=auth)
    body = r.json()
    check("history returns 200", r.status_code == 200, r.text)
    check("history has both messages", len(body["messages"]) == 2, str(body))
    check("history is oldest-first", body["messages"][0]["id"] == first_id)
    check("no next cursor on a short page", body["next_cursor"] is None)

    r = client.get(f"/conversations/{conv_id}/messages?limit=1", headers=auth)
    page1 = r.json()
    check("limit is respected", len(page1["messages"]) == 1)
    check("full page yields a cursor", page1["next_cursor"] == first_id)

    r = client.get(
        f"/conversations/{conv_id}/messages?limit=1&after_id={page1['next_cursor']}", headers=auth
    )
    page2 = r.json()
    check("cursor advances", page2["messages"][0]["id"] != first_id)

    print("\nisolation")
    r = client.post("/auth/signup", json={"email": other_email, "password": password})
    other_auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.get(f"/conversations/{conv_id}/messages", headers=other_auth)
    check("another user's conversation is 404, not 403", r.status_code == 404, r.text)

    r = client.post(
        f"/conversations/{conv_id}/messages", json={"content": "sneaking in"}, headers=other_auth
    )
    check("cannot post into another user's conversation", r.status_code == 404, r.text)

    r = client.get("/conversations", headers=other_auth)
    check("list is scoped to the caller", all(c["id"] != conv_id for c in r.json()))

    r = client.get(f"/conversations/{uuid.uuid4()}/messages", headers=auth)
    check("unknown conversation is 404", r.status_code == 404)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=None, help="hit a running server instead of in-process")
    args = ap.parse_args()

    if args.base_url:
        import httpx

        print(f"Driving {args.base_url}")
        with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
            run(client)
    else:
        from fastapi.testclient import TestClient

        from api.main import app

        print("Driving the app in-process (needs Postgres reachable at DATABASE_URL)")
        with TestClient(app) as client:
            run(client)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
