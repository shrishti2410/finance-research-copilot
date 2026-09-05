"""Tool registry: Python functions in, OpenAI function-calling schemas out.

The schema is *derived* from the function -- its signature, its type hints and
its docstring -- rather than written alongside it. A hand-maintained schema is a
second source of truth that drifts the first time an argument is renamed, and
the drift is silent: the model keeps sending the old name and the tool keeps
returning "unexpected keyword argument".

So `tools/*.py` docstrings are the contract. This module reads them.

Docstring format is Google style, which is what the tools already use:

    Summary line, and any prose before Args, becomes the tool description.

    Args:
        ticker: Text here becomes this parameter's description. Continuation
            lines are joined.

`Literal["a", "b"]` becomes a JSON Schema enum, which is the single most useful
thing a schema can carry -- it stops a model inventing `period="monthly"`.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Union, get_args, get_origin

from tools.base import ERROR_BAD_INPUT, ERROR_UNSUPPORTED, ERROR_UPSTREAM, error

_ARGS_HEADER = re.compile(r"^\s*(Args|Arguments|Parameters):\s*$")
_SECTION_HEADER = re.compile(r"^\s*(Returns|Raises|Yields|Examples?|Notes?):\s*$")
_ARG_LINE = re.compile(r"^\s{0,8}(\*{0,2}\w+)\s*(\([^)]*\))?\s*:\s*(.*)$")

_JSON_TYPES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def parse_docstring(fn: Callable) -> tuple[str, dict[str, str]]:
    """(description, {parameter: description}) from a Google-style docstring."""
    doc = inspect.getdoc(fn) or ""
    lines = doc.splitlines()

    description: list[str] = []
    params: dict[str, list[str]] = {}
    current: str | None = None
    in_args = False
    # The description is the prose *before* the first section header. Once any
    # header is seen it is over -- otherwise the Returns section falls back into
    # it, and every tool ships its whole output contract to the model as part of
    # its description.
    in_description = True

    for line in lines:
        if _ARGS_HEADER.match(line):
            in_args, current, in_description = True, None, False
            continue
        if _SECTION_HEADER.match(line):
            in_args, current, in_description = False, None, False
            continue

        if not in_args:
            if in_description:
                description.append(line)
            continue

        match = _ARG_LINE.match(line)
        if match and line.strip():
            current = match.group(1).lstrip("*")
            params[current] = [match.group(3).strip()]
        elif current and line.strip():
            params[current].append(line.strip())

    return (
        "\n".join(description).strip(),
        {name: " ".join(part for part in parts if part).strip() for name, parts in params.items()},
    )


def json_schema_for(annotation: Any) -> dict[str, Any]:
    """JSON Schema fragment for one annotation.

    Unknown annotations fall back to a bare string rather than raising: a tool
    with an exotic type should still be callable, just less precisely described.
    """
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {"type": "string"}

    origin = get_origin(annotation)

    if origin is Literal:
        values = list(get_args(annotation))
        kinds = {_JSON_TYPES.get(type(value), "string") for value in values}
        return {"type": kinds.pop() if len(kinds) == 1 else "string", "enum": values}

    if origin in (Union, getattr(__import__("types"), "UnionType", None)):
        # `str | None` is an optional string, not a union type in JSON Schema.
        non_null = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(non_null) == 1:
            return json_schema_for(non_null[0])
        return {"type": "string"}

    if origin in (list, set, tuple):
        args = get_args(annotation)
        return {"type": "array", "items": json_schema_for(args[0]) if args else {"type": "string"}}

    if origin is dict:
        return {"type": "object"}

    return {"type": _JSON_TYPES.get(annotation, "string")}


def coerce(value: Any, schema: dict[str, Any]) -> Any:
    """Nudge a value toward the type its schema declares.

    Models emit JSON-ish arguments, not JSON: `k: "3"` for an integer, `"true"`
    for a boolean, `2026` for a string. Every one of those is a plain TypeError
    inside the tool, which costs an iteration and reaches the model as an
    opaque failure it cannot fix -- in one live run the model read
    "TypeError: '<' not supported between str and int" as "the filing does not
    discuss this" and reported that to the user.

    Coercion only ever *adds* a conversion that would otherwise crash. Anything
    it cannot convert is passed through untouched, so the tool's own validation
    still produces the error message, which is written for a model to read.
    """
    declared = schema.get("type")

    if declared == "integer" and not isinstance(value, bool):
        if isinstance(value, str):
            try:
                return int(float(value.strip()))
            except ValueError:
                return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
    elif declared == "number" and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    elif declared == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1"):
            return True
        if lowered in ("false", "no", "0"):
            return False
    elif declared == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)

    return value


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable
    is_async: bool
    accepted: frozenset[str]
    required: frozenset[str]

    def schema(self) -> dict[str, Any]:
        """The OpenAI/Qwen `tools` entry for this function."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolResult:
    """What a tool call produced, plus what it cost."""

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    latency_ms: float
    ok: bool


def build_spec(fn: Callable, name: str | None = None, description: str | None = None,
               hidden: tuple[str, ...] = ()) -> ToolSpec:
    """Derive a ToolSpec from a function.

    `hidden` names parameters the model must not see -- an injected database
    connection, say. They stay callable from Python and absent from the schema.
    """
    signature = inspect.signature(fn)
    doc_description, doc_params = parse_docstring(fn)

    properties: dict[str, Any] = {}
    required: list[str] = []
    accepted: list[str] = []

    for param in signature.parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD) or param.name in hidden:
            continue
        accepted.append(param.name)

        schema = json_schema_for(param.annotation)
        if param.name in doc_params:
            schema["description"] = doc_params[param.name]
        if param.default is not param.empty and param.default is not None:
            schema["default"] = param.default
        properties[param.name] = schema

        if param.default is param.empty:
            required.append(param.name)

    return ToolSpec(
        name=name or fn.__name__,
        description=description or doc_description,
        parameters={"type": "object", "properties": properties, "required": required},
        fn=fn,
        is_async=inspect.iscoroutinefunction(fn),
        accepted=frozenset(accepted),
        required=frozenset(required),
    )


class Registry:
    """The tools an agent may call, and the one place they are called from."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, fn: Callable, name: str | None = None,
                 description: str | None = None, hidden: tuple[str, ...] = ()) -> ToolSpec:
        spec = build_spec(fn, name, description, hidden)
        self._tools[spec.name] = spec
        return spec

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._tools.values()]

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Execute a tool by name. Never raises.

        A raised exception here would abort the agent's turn, so every failure
        path -- unknown tool, bad arguments, a tool that raised despite its
        contract -- comes back as an error envelope the model can read and act
        on.
        """
        started = time.perf_counter()
        arguments = arguments if isinstance(arguments, dict) else {}

        def finish(result: dict[str, Any]) -> ToolResult:
            return ToolResult(
                name=name,
                arguments=arguments,
                result=result,
                latency_ms=(time.perf_counter() - started) * 1000,
                ok=bool(result.get("ok")),
            )

        spec = self._tools.get(name)
        if spec is None:
            return finish(error(
                ERROR_UNSUPPORTED,
                f"No tool named {name!r}. Available tools: {', '.join(self.names)}.",
            ))

        # Unknown arguments are dropped rather than rejected. Models invent
        # plausible extras, and inside a five-iteration budget, spending a turn
        # on "you passed an argument I do not accept" is worse than ignoring it
        # -- the required ones below are still enforced.
        properties = spec.parameters.get("properties", {})
        accepted = {
            key: coerce(value, properties.get(key, {}))
            for key, value in arguments.items() if key in spec.accepted
        }
        ignored = sorted(set(arguments) - spec.accepted)

        missing = sorted(spec.required - accepted.keys())
        if missing:
            return finish(error(
                ERROR_BAD_INPUT,
                f"{name} requires {', '.join(missing)}. "
                f"It accepts: {', '.join(sorted(spec.accepted))}.",
            ))

        try:
            if spec.is_async:
                result = await spec.fn(**accepted)
            else:
                # Every sync tool here does blocking network I/O. Calling it
                # directly would stall the event loop, and with it every other
                # request this process is serving.
                result = await asyncio.to_thread(spec.fn, **accepted)
        except Exception as exc:  # noqa: BLE001 - a tool must never kill the loop
            return finish(error(
                ERROR_UPSTREAM,
                f"{name} raised {type(exc).__name__}: {exc}",
            ))

        if not isinstance(result, dict):
            return finish(error(
                ERROR_UPSTREAM,
                f"{name} returned {type(result).__name__}, not a result dict.",
            ))
        if ignored:
            result = {**result, "ignored_arguments": ignored}
        return finish(result)
