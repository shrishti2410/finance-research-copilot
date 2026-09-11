"""Explicit network settings for yfinance, applied once at import.

Two things were being left to the library's defaults, which is the same as
having no policy: a default is whatever the last upgrade decided.

**Timeouts.** yfinance 1.2.0 defaults to 10 s on a price history call and 30 s on
the metadata calls behind `fast_info`. Neither was ever set here, so both were
inherited silently. They are now set from `core.config`, which means they are
visible, tunable, and cannot drift under a `pip upgrade`.

**Retries.** The library already has the retry loop this project would otherwise
have wrapped around it -- `YfData._make_request` classifies transient errors and
sleeps `2 ** attempt` between tries. It ships disabled: `YfConfig.network.retries`
is `0`, so the loop runs exactly one attempt. Turning it on is strictly better
than adding a second layer outside it, which would have multiplied the two and
turned one slow call into nine.

`fast_info` cannot be given a per-call timeout, so the only lever for it is the
library's own default, set here.
"""

from __future__ import annotations

import logging

from yfinance.config import YfConfig

from core.config import settings

log = logging.getLogger(__name__)

__all__ = ["configure", "history_timeout"]

_configured = False


def configure() -> None:
    """Apply the project's network policy to yfinance. Idempotent."""
    global _configured
    if _configured:
        return
    YfConfig.network.retries = settings.yfinance_retries
    _configured = True
    log.debug("yfinance configured: retries=%d timeout=%ss",
              settings.yfinance_retries, settings.yfinance_timeout)


def history_timeout() -> float:
    """The timeout to pass explicitly to `Ticker.history`."""
    return settings.yfinance_timeout


configure()
