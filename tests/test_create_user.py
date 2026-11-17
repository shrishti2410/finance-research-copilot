"""scripts/create_user.py: an account made at the prompt can sign in through
the API, the password is never printed, and an existing email is refused.
Live Postgres; skips when it is unreachable."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PASSWORD = "a-long-test-password-" + uuid.uuid4().hex[:6]


def load():
    path = Path(__file__).resolve().parent.parent / "scripts" / "create_user.py"
    spec = importlib.util.spec_from_file_location("create_user", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(monkeypatch):
    from api.main import app
    from core.config import settings

    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    with TestClient(app) as c:
        if c.get("/health/db").status_code != 200:
            pytest.skip("Postgres unreachable")
        yield c


def run_script(monkeypatch, capsys, email, answers):
    script = load()
    replies = iter(answers)
    monkeypatch.setattr(script.getpass, "getpass", lambda prompt="": next(replies))
    monkeypatch.setattr(sys, "argv", ["create_user.py", email, "--name", "Owner"])
    try:
        code = script.main()
    except SystemExit as exc:
        code = exc.code
    out = capsys.readouterr()
    return code, out.out + out.err


def test_an_account_made_by_the_script_signs_in(client, monkeypatch, capsys):
    email = f"Owner-{uuid.uuid4().hex[:8]}@Example.com"
    code, output = run_script(monkeypatch, capsys, email, [PASSWORD, PASSWORD])
    assert code == 0 and PASSWORD not in output
    login = client.post("/auth/login", json={"email": email.lower(), "password": PASSWORD})
    assert login.status_code == 200
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    assert me.json()["email"] == email.lower() and me.json()["display_name"] == "Owner"

    again, output = run_script(monkeypatch, capsys, email, [PASSWORD, PASSWORD])
    assert again != 0 and "already has an account" in str(again) + output


@pytest.mark.parametrize("answers, why", [
    (["short", "short"], "at least"),
    ([PASSWORD, PASSWORD + "x"], "differ"),
])
def test_a_bad_password_creates_nothing(client, monkeypatch, capsys, answers, why):
    email = f"nobody-{uuid.uuid4().hex[:8]}@example.com"
    code, output = run_script(monkeypatch, capsys, email, answers)
    assert why in str(code) + output
    assert client.post("/auth/login", json={"email": email, "password": answers[0]}
                       ).status_code == 401
