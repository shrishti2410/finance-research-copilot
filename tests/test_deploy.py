"""What a public deployment adds: boot-time refusal of unsafe settings, a hosted
Postgres URL that works with asyncpg, readiness, the demo account, and the
daily caps that keep visitors from spending the provider allowance.

Settings tests build their own `Settings` and need nothing running. The API
and quota tests run against live Postgres, migrated to head, and skip when it
is unreachable.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import quotas
from api.main import MIGRATION_HEAD, app
from core.config import Settings, settings

SECRET = "s" * 48

GOOD = dict(
    _env_file=None,
    environment="production",
    jwt_secret=SECRET,
    cors_origins="https://copilot-demo.vercel.app",
    database_url="postgresql://app:pw@ep-cool-name.eu-central-1.aws.neon.tech/neondb"
                 "?sslmode=require&channel_binding=require",
    redis_url="rediss://default:tok@example.upstash.io:6379",
    sec_edgar_user_agent="copilot/1.0 (you@yourdomain.com)",
    agent_provider="groq",
    groq_api_key="test-key-not-a-real-one",
    hosted_tokens_per_day=120_000,
)


# ── settings ──────────────────────────────────────────────────────────────────

def test_a_hosted_postgres_url_is_rewritten_for_asyncpg():
    s = Settings(**GOOD)
    assert s.database_url == (
        "postgresql+asyncpg://app:pw@ep-cool-name.eu-central-1.aws.neon.tech/neondb")
    assert s.database_connect_args == {"ssl": "require"}


@pytest.mark.parametrize("url, args", [
    ("postgres://u:p@h/db?sslmode=verify-full", {"ssl": True}),
    ("postgresql+asyncpg://u:p@h/db", {}),
    ("postgresql://u:p@h/db?sslmode=disable&application_name=x", {}),
])
def test_sslmode_becomes_asyncpgs_ssl_argument(url, args):
    s = Settings(_env_file=None, database_url=url)
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert "sslmode" not in s.database_url and s.database_connect_args == args


def test_the_local_default_still_boots_in_development():
    s = Settings(_env_file=None)
    assert s.environment == "development" and s.database_connect_args == {}


def test_a_correct_production_configuration_boots():
    assert Settings(**GOOD).environment == "production"


@pytest.mark.parametrize("change, named", [
    ({"jwt_secret": "dev-only-insecure-change-me"}, "JWT_SECRET"),
    ({"jwt_secret": "short"}, "JWT_SECRET"),
    ({"cors_origins": ""}, "CORS_ORIGINS"),
    ({"cors_origins": "*"}, "CORS_ORIGINS"),
    ({"cors_origins": "http://copilot-demo.vercel.app"}, "CORS_ORIGINS"),
    ({"cors_origins": "https://copilot-demo.vercel.app/"}, "CORS_ORIGINS"),
    ({"database_url": "postgresql://u:p@127.0.0.1:5432/db?sslmode=require"}, "DATABASE_URL"),
    ({"database_url": "postgresql://u:p@db.example.com/db"}, "DATABASE_URL"),
    ({"redis_url": "redis://127.0.0.1:6379/0"}, "REDIS_URL"),
    ({"sec_edgar_user_agent": "finance-research-copilot (contact: set SEC_EDGAR_USER_AGENT)"},
     "SEC_EDGAR_USER_AGENT"),
    ({"rate_limit_enabled": False}, "RATE_LIMIT_ENABLED"),
    ({"hosted_tokens_per_day": 0}, "HOSTED_TOKENS_PER_DAY"),
    ({"demo_enabled": True, "demo_questions_per_day": 0}, "DEMO_QUESTIONS_PER_DAY"),
])
def test_production_refuses_an_unsafe_setting_and_names_it(change, named):
    with pytest.raises(ValueError) as caught:
        Settings(**{**GOOD, **change})
    message = str(caught.value)
    assert named in message
    for secret in (SECRET, "test-key-not-a-real-one", "pw@"):
        assert secret not in message, "a refusal must not echo a secret"


def test_every_problem_is_listed_at_once():
    with pytest.raises(ValueError) as caught:
        Settings(_env_file=None, environment="production")
    message = str(caught.value)
    for named in ("JWT_SECRET", "CORS_ORIGINS", "DATABASE_URL", "REDIS_URL",
                  "SEC_EDGAR_USER_AGENT"):
        assert named in message


def test_the_same_settings_are_only_warnings_in_development():
    s = Settings(_env_file=None, cors_origins="*", hosted_tokens_per_day=0)
    assert s.environment == "development"


# ── the API ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        if c.get("/health/db").status_code != 200:
            pytest.skip("Postgres unreachable; run migrations first")
        yield c


@pytest.fixture
def public(monkeypatch):
    """The settings a public demo runs with, on this process's settings object."""
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "demo_enabled", True)
    monkeypatch.setattr(settings, "signup_enabled", False)
    yield


def test_readiness_reports_the_migration_the_code_expects(client):
    body = client.get("/health/ready").json()
    assert MIGRATION_HEAD == "0004_public_demo"
    if body["migration"] != MIGRATION_HEAD:
        pytest.skip(f"database is at {body['migration']}; run alembic upgrade head")
    assert body["status"] == "ok" and body["database"] == "ok"


def test_options_say_what_the_sign_in_page_should_offer(client, public):
    assert client.get("/auth/options").json() == {
        "signup": False, "demo": True,
        "demo_questions_per_account": settings.demo_questions_per_account}


def test_the_demo_hands_out_a_fresh_isolated_account(client, public):
    first = client.post("/auth/demo")
    second = client.post("/auth/demo")
    assert first.status_code == second.status_code == 201
    me = [client.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
          .json() for r in (first, second)]
    assert me[0]["id"] != me[1]["id"], "one account per visit, not one shared account"
    assert all(m["email"].endswith("@demo.example.com") for m in me)


def test_the_demo_is_not_found_when_it_is_off(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "demo_enabled", False)
    assert client.post("/auth/demo").status_code == 404


def test_signup_can_be_closed(client, public):
    response = client.post("/auth/signup", json={
        "email": f"closed-{uuid.uuid4().hex[:8]}@example.com", "password": "correct-horse-battery"})
    assert response.status_code == 403 and "demo" in response.json()["detail"]


def test_signup_refuses_the_demo_domain(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "signup_enabled", True)
    response = client.post("/auth/signup", json={
        "email": "demo-impostor@demo.example.com", "password": "correct-horse-battery"})
    assert response.status_code == 422


def test_cors_allows_only_the_configured_origin_and_no_credentials(client):
    allowed = settings.cors_origin_list[0]
    ok = client.options("/auth/options", headers={
        "Origin": allowed, "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == allowed
    assert "access-control-allow-credentials" not in ok.headers
    other = client.options("/auth/options", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert other.headers.get("access-control-allow-origin") is None


# ── the caps ──────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def _user(demo: bool):
    return SimpleNamespace(id=uuid.uuid4(), is_demo=demo)


@pytest.fixture
def counters(client, monkeypatch):
    """A clean slate: today's counters for the shared scopes start empty."""
    from sqlalchemy import text

    from db.base import engine, session_scope

    async def clear():
        # Connections pooled by an earlier event loop (the TestClient's) cannot
        # be used from this one; start from an empty pool.
        await engine.dispose()
        async with session_scope() as session:
            await session.execute(text(
                "DELETE FROM usage_counters WHERE day = :day AND key = 'all'"),
                {"day": quotas._today().date()})
            await session.commit()
        await engine.dispose()

    try:
        run(clear())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"usage_counters not available ({type(exc).__name__}: {exc}); "
                    f"run migrations")
    for name in ("demo_questions_per_account", "demo_questions_per_day",
                 "user_questions_per_day", "hosted_tokens_per_day"):
        monkeypatch.setattr(settings, name, 0)
    yield
    run(clear())


def _admit(user):
    from db.base import engine

    async def go():
        await engine.dispose()
        try:
            await quotas.admit(user)
            return 200
        except Exception as exc:  # noqa: BLE001
            return exc
        finally:
            await engine.dispose()
    return run(go())


def test_no_cap_means_no_database_work(monkeypatch):
    for name in ("demo_questions_per_account", "demo_questions_per_day",
                 "user_questions_per_day", "hosted_tokens_per_day"):
        monkeypatch.setattr(settings, name, 0)
    monkeypatch.setattr(quotas, "session_scope", None)   # would raise if used
    run(quotas.admit(_user(demo=True)))


def test_a_demo_account_gets_its_daily_questions_then_a_429(counters, monkeypatch):
    monkeypatch.setattr(settings, "demo_questions_per_account", 2)
    user = _user(demo=True)
    assert _admit(user) == 200 and _admit(user) == 200
    refused = _admit(user)
    assert refused.status_code == 429 and "2 questions" in refused.detail
    assert 0 < int(refused.headers["Retry-After"]) <= 86_400
    assert _admit(_user(demo=True)) == 200, "another demo account has its own"


def test_all_demo_accounts_share_one_daily_cap(counters, monkeypatch):
    monkeypatch.setattr(settings, "demo_questions_per_account", 5)
    monkeypatch.setattr(settings, "demo_questions_per_day", 3)
    results = [_admit(_user(demo=True)) for _ in range(4)]
    assert results[:3] == [200, 200, 200] and results[3].status_code == 429


def test_a_refusal_by_the_shared_cap_does_not_use_up_the_accounts_own(counters, monkeypatch):
    monkeypatch.setattr(settings, "demo_questions_per_account", 1)
    monkeypatch.setattr(settings, "demo_questions_per_day", 1)
    assert _admit(_user(demo=True)) == 200
    user = _user(demo=True)
    assert _admit(user).status_code == 429
    monkeypatch.setattr(settings, "demo_questions_per_day", 2)
    assert _admit(user) == 200, "the refused attempt was rolled back"


def test_concurrent_questions_cannot_both_take_the_last_slot(counters, monkeypatch):
    monkeypatch.setattr(settings, "demo_questions_per_account", 3)
    user = _user(demo=True)
    from db.base import engine

    async def many():
        async def one():
            try:
                await quotas.admit(user)
                return True
            except Exception:  # noqa: BLE001
                return False
        await engine.dispose()
        try:
            return await asyncio.wait_for(asyncio.gather(*(one() for _ in range(8))), 30)
        finally:
            await engine.dispose()
    assert sum(run(many())) == 3


def test_other_accounts_have_their_own_cap(counters, monkeypatch):
    monkeypatch.setattr(settings, "user_questions_per_day", 1)
    user = _user(demo=False)
    assert _admit(user) == 200 and _admit(user).status_code == 429
    assert _admit(_user(demo=True)) == 200, "the user cap is not the demo's"


def test_the_hosted_token_budget_stops_new_questions_once_spent(counters, monkeypatch):
    monkeypatch.setattr(settings, "agent_provider", "groq")
    monkeypatch.setattr(settings, "hosted_tokens_per_day", 10_000)
    from db.base import engine

    async def spend(tokens):
        try:
            await quotas.record_usage(SimpleNamespace(
                provider="groq", router_provider="groq",
                prompt_tokens=tokens, completion_tokens=0))
        finally:
            await engine.dispose()

    user = _user(demo=False)
    run(spend(6_000))
    assert _admit(user) == 200
    run(spend(4_500))
    refused = _admit(user)
    assert refused.status_code == 429 and "model budget" in refused.detail


def test_local_model_tokens_are_not_counted(counters, monkeypatch):
    monkeypatch.setattr(settings, "hosted_tokens_per_day", 10)
    run(quotas.record_usage(SimpleNamespace(
        provider="ollama", router_provider="ollama", prompt_tokens=10_000, completion_tokens=0)))
    monkeypatch.setattr(settings, "agent_provider", "groq")
    assert _admit(_user(demo=False)) == 200


def test_the_reset_is_the_next_utc_midnight():
    from datetime import datetime, timezone
    assert quotas.seconds_until_reset(datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)) == 3600


def test_every_placeholder_in_the_free_tier_example_is_refused_at_boot():
    """deploy/free-tier.env.example is copied into a dashboard by hand. A value
    left as it is must stop the service, not quietly work (CLAUDE.md rule 5)."""
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "deploy" / "free-tier.env.example"
    values = {}
    for line in example.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip()
    assert values["environment"] == "production"
    with pytest.raises(ValueError) as caught:
        Settings(_env_file=None, **values)
    message = str(caught.value)
    for named in ("JWT_SECRET", "DATABASE_URL", "REDIS_URL", "SEC_EDGAR_USER_AGENT",
                  "CORS_ORIGINS", "HOSTED_TOKENS_PER_DAY", "DEMO_QUESTIONS_PER_DAY",
                  "GROQ_API_KEY"):
        assert named in message, named


def test_a_failed_boot_never_prints_the_values_it_was_given():
    """Pydantic puts the input into a validation error by default: every setting,
    secrets included, in the deploy log."""
    secrets_given = {"jwt_secret": "jwt-" + "q" * 40, "groq_api_key": "gsk-not-real-" + "z" * 20,
                     "database_url": "postgresql://app:hunter2-db-password@127.0.0.1/db"}
    with pytest.raises(ValueError) as caught:
        Settings(_env_file=None, environment="production", agent_provider="groq",
                 **{**secrets_given, "groq_api_key": secrets_given["groq_api_key"]})
    for value in ("q" * 40, "z" * 20, "hunter2-db-password"):
        assert value not in str(caught.value)
    with pytest.raises(ValueError) as caught:
        Settings(_env_file=None, jwt_secret="jwt-" + "q" * 40, db_pool_size="not-a-number")
    assert "q" * 40 not in str(caught.value)


# -- embeddings on a fractional CPU --------------------------------------------

def test_the_embedding_thread_count_is_applied_when_the_encoder_loads(monkeypatch):
    import sys
    import types

    calls = []
    fake_torch = types.SimpleNamespace(set_num_threads=calls.append,
                                       set_grad_enabled=lambda flag: None)

    class _Model:
        def eval(self):
            return self

    fake_transformers = types.SimpleNamespace(
        AutoTokenizer=types.SimpleNamespace(from_pretrained=lambda model_id: "tokenizer"),
        AutoModel=types.SimpleNamespace(from_pretrained=lambda model_id: _Model()))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    from rag import embeddings
    monkeypatch.setattr(settings, "embedding_threads", 1)
    embeddings.get_encoder.cache_clear()
    try:
        embeddings.get_encoder("test-model")
        assert calls == [1]
        monkeypatch.setattr(settings, "embedding_threads", 0)
        embeddings.get_encoder.cache_clear()
        embeddings.get_encoder("test-model")
        assert calls == [1], "0 leaves torch's default alone"
    finally:
        embeddings.get_encoder.cache_clear()


def test_the_free_tier_example_runs_one_embedding_thread_and_preloads():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "deploy" / "free-tier.env.example"
            ).read_text(encoding="utf-8")
    assert "\nEMBEDDING_THREADS=1\n" in text and "\nEMBEDDING_PRELOAD=true\n" in text
