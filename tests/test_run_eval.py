"""Tests for the eval harness itself.

The harness is code that produces numbers people will believe, so its own bugs
are expensive: the first full run reported 12.5% accuracy, and 25 of the 40
cases had never executed. These pin the failure that caused it.
"""

import httpx
import pytest

from eval.run_eval import Session, ask


class FakeAPI:
    """A server whose token expires after `good_for` authorised calls."""

    def __init__(self, good_for: int):
        self.good_for = good_for
        self.used = 0
        self.logins = 0
        self.token = "token-1"
        self.asked: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/signup":
            return httpx.Response(201, json={"access_token": self.token})
        if path == "/auth/login":
            self.logins += 1
            self.used = 0
            self.token = f"token-{self.logins + 1}"
            return httpx.Response(200, json={"access_token": self.token})

        presented = request.headers.get("Authorization", "")
        if presented != f"Bearer {self.token}" or self.used >= self.good_for:
            return httpx.Response(401, json={"detail": "Not authenticated."})
        self.used += 1

        if path == "/conversations":
            return httpx.Response(201, json={"id": "c-1"})
        if path == "/ask":
            self.asked.append("q")
            return httpx.Response(200, json={
                "answer": "71.07%", "completed": True, "iterations": 1,
                "stop_reason": "final_answer", "total_ms": 1.0, "steps": [],
            })
        return httpx.Response(404, json={"detail": "nope"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler),
                            base_url="http://api.invalid")


def test_a_long_run_survives_its_token_expiring():
    """The bug that voided the first full run: one token, held for 100 minutes
    against a 30-minute expiry, so every case after minute 30 got a 401."""
    api = FakeAPI(good_for=4)          # two calls per question
    with api.client() as client:
        session = Session(client)
        for _ in range(6):
            ask(session, "What was revenue?")

    assert len(api.asked) == 6, "every question must reach /ask"
    assert api.logins >= 2, "the harness must have re-authenticated"


def test_no_refresh_happens_while_the_token_is_good():
    """Re-authenticating per request would work and would also hide a broken
    token, so the refresh has to be driven by an actual 401."""
    api = FakeAPI(good_for=1000)
    with api.client() as client:
        session = Session(client)
        for _ in range(3):
            ask(session, "q")

    assert api.logins == 0


def test_a_persistent_401_raises_rather_than_looping():
    """A second 401 means the credentials are wrong, not stale. Retrying that
    forever turns a configuration error into a hang."""
    api = FakeAPI(good_for=0)
    with api.client() as client:
        session = Session(client)
        with pytest.raises(httpx.HTTPStatusError):
            ask(session, "q")

    assert api.logins == 1, "exactly one retry, not a loop"


def test_each_question_gets_its_own_conversation():
    """Sharing one thread would feed each answer into the next question's
    history window, scoring a system that had been told the earlier answers."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/signup":
            return httpx.Response(201, json={"access_token": "t"})
        if request.url.path == "/conversations":
            seen.append({"new": True})
            return httpx.Response(201, json={"id": f"c-{len(seen)}"})
        return httpx.Response(200, json={
            "answer": "1%", "completed": True, "iterations": 1,
            "stop_reason": "final_answer", "total_ms": 1.0, "steps": [],
        })

    with httpx.Client(transport=httpx.MockTransport(handler),
                      base_url="http://api.invalid") as client:
        session = Session(client)
        ask(session, "one")
        ask(session, "two")

    assert len(seen) == 2
