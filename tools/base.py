"""Shared result envelope for tools.

Every tool returns a plain `dict`, never raises for an expected failure, and
never returns a bare `None`. That is a contract with the model calling it, not
tidiness: an exception crossing the tool boundary aborts the agent's turn, and a
`None` gets stringified into the transcript as "None", which the model will
happily reason over as if it were data.

So a failure is a *value*:

    {"ok": False, "error": "no_data", "message": "...", "ticker": "ZZZZ"}

`error` is a stable machine-readable code the agent can branch on; `message` is
the sentence a model should read. Both are always present on a failure, and
`ok` is always present on both paths so a caller can check one key.
"""

from __future__ import annotations

from typing import Any

# Stable error codes. Kept small on purpose -- a code the agent cannot act on
# differently from another code should not be its own code.
ERROR_BAD_INPUT = "bad_input"        # the call itself was malformed; do not retry as-is
ERROR_NO_DATA = "no_data"            # the source answered, and has nothing for this request
ERROR_UNSUPPORTED = "unsupported"    # valid request, not something this tool can do
ERROR_UPSTREAM = "upstream_error"    # the source failed or was unreachable; a retry may work

ERROR_CODES = (ERROR_BAD_INPUT, ERROR_NO_DATA, ERROR_UNSUPPORTED, ERROR_UPSTREAM)


def ok(**fields: Any) -> dict[str, Any]:
    """A successful tool result."""
    return {"ok": True, **fields}


def error(code: str, message: str, **fields: Any) -> dict[str, Any]:
    """A failed tool result.

    `message` is written for the model to read and should say what to do next
    where there is something to do -- "check the ticker symbol", "the market was
    closed on that date" -- because the model's only options are to retry
    differently, try another tool, or tell the user.
    """
    return {"ok": False, "error": code, "message": message, **fields}
