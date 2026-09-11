"""Retry with backoff, for the external APIs that do not do it themselves.

Scope, deliberately narrow
--------------------------
This is for **yfinance's news feed, Google News RSS and SEC EDGAR** -- third-party
HTTP that fails transiently: a dropped connection, a momentary 429, a 503 while a
load balancer reshuffles. Retrying those turns a spurious failure into a slightly
slower success.

It is **not** for model inference, and that exclusion is the point rather than an
oversight. A stuck inference call is not a blip: on this CPU host a single answer
step legitimately runs 15-30 s and the read timeout is 300 s, so a retry does not
recover a hung request, it waits 300 s and then waits 300 s again. Worse, the
agent loop makes up to five of these per question. An inference call that fails
must fail, once, with a message the caller can act on -- which is what
`agent/orchestrator.py` does.

It is also not for yfinance's price and fundamentals calls, for a different
reason: yfinance 1.2.0 already has this exact loop inside `YfData._make_request`,
complete with `2 ** attempt` backoff and a transient-error classifier. Wrapping it
would multiply the two -- our three attempts times its own -- and turn one slow
call into nine. What that library needed was not another retry layer but for its
own to be switched on: `YfConfig.network.retries` ships as `0`. See
`tools/_yahoo.py`.

Backoff
-------
Exponential from `base`, with full jitter. The jitter matters more than it looks:
the agent issues parallel tool calls, so two `search_news` calls can fail against
the same rate limit in the same millisecond, and a fixed schedule would have them
retry together and collide again.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, Iterable, TypeVar

log = logging.getLogger(__name__)

__all__ = ["retrying", "RetryPolicy", "DEFAULT_ATTEMPTS", "DEFAULT_BASE_DELAY"]

T = TypeVar("T")

# Three attempts, not more. These calls sit inside a user's question: a fourth
# attempt adds seconds to a request that is already failing, and by then the
# upstream is down rather than blipping.
DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 8.0


class RetryPolicy:
    """What to retry, how often, and how long to wait between tries."""

    def __init__(self, *, attempts: int = DEFAULT_ATTEMPTS,
                 base_delay: float = DEFAULT_BASE_DELAY,
                 max_delay: float = DEFAULT_MAX_DELAY,
                 retry_on: tuple[type[BaseException], ...] = (Exception,),
                 give_up_on: tuple[type[BaseException], ...] = (),
                 predicate: Callable[[BaseException], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if attempts < 1:
            raise ValueError(f"attempts must be at least 1, got {attempts}")
        self.attempts = attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retry_on = retry_on
        self.give_up_on = give_up_on
        # Class membership is not always enough to decide. An HTTPError is one
        # exception type carrying every status code, and 429 and 404 want
        # opposite answers -- so a caller can supply the judgement instead.
        self.predicate = predicate
        self.sleep = sleep

    def should_retry(self, exc: BaseException) -> bool:
        # give_up_on wins, so a caller can retry a broad category while carving
        # out the permanent failures inside it -- a 404 is a RequestException
        # too, and asking for it three times does not make it exist.
        if isinstance(exc, self.give_up_on):
            return False
        if self.predicate is not None:
            return self.predicate(exc)
        return isinstance(exc, self.retry_on)

    def delay_for(self, attempt: int) -> float:
        """Full jitter: uniform in [0, min(max, base * 2**attempt)].

        Parallel tool calls that fail together must not retry together.
        """
        ceiling = min(self.max_delay, self.base_delay * (2 ** attempt))
        return random.uniform(0, ceiling)


def retrying(call: Callable[[], T], *, policy: RetryPolicy | None = None,
             description: str = "call") -> T:
    """Run `call`, retrying transient failures, and re-raise the last one.

    The exception that escapes is the last attempt's, not a wrapper: the tools
    turn it into their own error envelope and the message needs to say what
    actually went wrong.
    """
    policy = policy or RetryPolicy()
    last: BaseException | None = None

    for attempt in range(policy.attempts):
        try:
            return call()
        except BaseException as exc:  # noqa: BLE001 - classified by the policy
            last = exc
            final = attempt == policy.attempts - 1
            if final or not policy.should_retry(exc):
                raise
            delay = policy.delay_for(attempt)
            log.warning(
                "%s failed (%s: %s); retrying in %.1fs [attempt %d of %d]",
                description, type(exc).__name__, str(exc)[:160], delay,
                attempt + 2, policy.attempts,
            )
            policy.sleep(delay)

    # Unreachable: the loop either returns or raises. Here so a future edit that
    # breaks that invariant fails loudly rather than returning None.
    raise AssertionError(f"retrying({description}) fell through")  # pragma: no cover


def status_is_transient(status_code: int) -> bool:
    """Whether an HTTP status is worth trying again.

    429 and 5xx only. A 4xx that is not 429 is the request being wrong, and
    repeating it wastes the user's time to reach the same answer -- except 408
    and 425, which are the server asking for exactly that.
    """
    return status_code in (408, 425, 429) or 500 <= status_code < 600


def transient_statuses(codes: Iterable[int]) -> tuple[int, ...]:
    return tuple(code for code in codes if status_is_transient(code))
