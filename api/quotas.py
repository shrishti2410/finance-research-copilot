"""Daily caps for a public deployment: questions per account, and hosted tokens.

A public demo is a stranger with a button that spends the operator's provider
allowance. Groq's free tier for openai/gpt-oss-120b is 200,000 tokens a day
(measured from its 429 on 2026-11-15), and one question costs several thousand.
Without a cap, a handful of visitors empty it for the day, and the next person
who arrives, or the developer running an eval on the same key, gets nothing.

Three caps, each per UTC day, each off when its setting is 0 (the default, so
development, the test suite and the eval harness are untouched):

    DEMO_QUESTIONS_PER_ACCOUNT   one demo account
    DEMO_QUESTIONS_PER_DAY       all demo accounts together
    USER_QUESTIONS_PER_DAY       any other account
    HOSTED_TOKENS_PER_DAY        tokens sent to a hosted provider, all users

How they are enforced
---------------------
In Postgres, in `usage_counters`, keyed by (day, scope, key). Not Redis: the
free Redis tier this deploys to counts every command against a monthly
allowance and its data is not the thing to lose, and these are a few writes per
question, which Postgres absorbs without noticing.

A question is admitted by incrementing its counters only while they are under
their caps, in one transaction (`admit`). Two concurrent requests cannot both
take the last slot: the increment is a single conditional UPSERT. A question
refused by a later counter takes back the earlier one, because the transaction
rolls back.

Tokens are counted after the run (`record_usage`), because nobody knows a
run's cost before it. So the token cap refuses new questions once the day's
total has reached it, and the overshoot is bounded by the questions already
running at that moment, a few thousand tokens each.

A refused question is a 429 with Retry-After set to the next UTC midnight.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import text

from core.config import settings
from db.base import session_scope

log = logging.getLogger(__name__)

# One row per (day, scope, key). See the module docstring.
SCOPE_DEMO_ACCOUNT = "demo_account"
SCOPE_DEMO_ALL = "demo_all"
SCOPE_USER = "user"
SCOPE_HOSTED_TOKENS = "hosted_tokens"
ALL = "all"

_RESERVE = text("""
    INSERT INTO usage_counters (day, scope, key, questions, tokens)
    VALUES (:day, :scope, :key, 1, 0)
    ON CONFLICT (day, scope, key)
    DO UPDATE SET questions = usage_counters.questions + 1
    WHERE usage_counters.questions < :cap
    RETURNING questions
""")

_ADD_TOKENS = text("""
    INSERT INTO usage_counters (day, scope, key, questions, tokens)
    VALUES (:day, :scope, :key, 0, :tokens)
    ON CONFLICT (day, scope, key)
    DO UPDATE SET tokens = usage_counters.tokens + :tokens
""")

_TOKENS_TODAY = text("""
    SELECT tokens FROM usage_counters WHERE day = :day AND scope = :scope AND key = :key
""")


def _today() -> datetime:
    return datetime.now(timezone.utc)


def seconds_until_reset(now: datetime | None = None) -> int:
    """Seconds to the next UTC midnight, when every cap resets."""
    now = now or _today()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


def _refuse(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(seconds_until_reset())},
    )


def _caps_for(user) -> list[tuple[str, str, int, str]]:
    """(scope, key, cap, what to tell the user) for every question cap that applies."""
    caps = []
    if getattr(user, "is_demo", False):
        if settings.demo_questions_per_account:
            caps.append((SCOPE_DEMO_ACCOUNT, str(user.id), settings.demo_questions_per_account,
                         f"This demo account has used its {settings.demo_questions_per_account} "
                         f"questions for today."))
        if settings.demo_questions_per_day:
            caps.append((SCOPE_DEMO_ALL, ALL, settings.demo_questions_per_day,
                         "The demo has answered as many questions as it can today."))
    elif settings.user_questions_per_day:
        caps.append((SCOPE_USER, str(user.id), settings.user_questions_per_day,
                     f"You have used your {settings.user_questions_per_day} questions "
                     f"for today."))
    return caps


async def admit(user) -> None:
    """Admit one question for `user`, or raise a 429 saying which cap it hit.

    Does nothing, and touches no database, when no cap applies.
    """
    caps = _caps_for(user)
    budget = settings.hosted_tokens_per_day if settings.hosted else 0
    if not caps and not budget:
        return

    day = _today().date()
    refused: str | None = None
    async with session_scope() as session:
        if budget:
            used = await session.scalar(_TOKENS_TODAY, {
                "day": day, "scope": SCOPE_HOSTED_TOKENS, "key": ALL})
            if (used or 0) >= budget:
                refused = ("The demo has used today's model budget. It resets at "
                           "00:00 UTC.")
        if refused is None:
            for scope, key, cap, message in caps:
                taken = await session.scalar(_RESERVE, {
                    "day": day, "scope": scope, "key": key, "cap": cap})
                if taken is None:
                    refused = f"{message} The limit resets at 00:00 UTC."
                    break
        if refused is None:
            await session.commit()
        else:
            await session.rollback()
    if refused is not None:
        log.info("quota: refused a question for user=%s: %s", user.id, refused)
        raise _refuse(refused)


async def record_usage(result) -> None:
    """Add a finished run's hosted-provider tokens to today's total.

    Never raises: a failure to count is logged, not turned into a failed
    answer the user has already been shown.
    """
    if not settings.hosted or not settings.hosted_tokens_per_day:
        return
    if "ollama" == getattr(result, "provider", None) == getattr(result, "router_provider", None):
        return
    tokens = int((result.prompt_tokens or 0) + (result.completion_tokens or 0))
    if tokens <= 0:
        return
    try:
        async with session_scope() as session:
            await session.execute(_ADD_TOKENS, {
                "day": _today().date(), "scope": SCOPE_HOSTED_TOKENS, "key": ALL,
                "tokens": tokens})
            await session.commit()
    except Exception:  # noqa: BLE001 - see the docstring
        log.exception("quota: could not record %d hosted tokens", tokens)
