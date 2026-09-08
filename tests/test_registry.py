"""Tests for schema derivation and tool dispatch.

The schemas the model sees are generated from the tools' docstrings and type
hints, so these tests are what stop a renamed argument from silently producing a
schema the model can no longer satisfy.
"""

import asyncio
from typing import Literal

import pytest

from tools.base import ERROR_BAD_INPUT, ERROR_UNSUPPORTED, ERROR_UPSTREAM, ok
from tools.registry import Registry, build_spec, coerce, json_schema_for, parse_docstring


def sample(
    ticker: str,
    ratio_name: Literal["gross_margin", "pe_ratio"],
    period: str = "annual",
    limit: int = 5,
    flag: bool = False,
    company: str | None = None,
) -> dict:
    """Calculate something for a company.

    A second line of prose that is also part of the description.

    Args:
        ticker: Stock ticker symbol, e.g. "NVDA". Case-insensitive.
        ratio_name: Which ratio to compute.
        period: Which reporting period to use. One of "annual" or
            "quarterly", continuing onto a second line.
        limit: How many to return.
        flag: A boolean switch.
        company: Optional company filter.

    Returns:
        Something that should not appear in the description.
    """
    return ok(ticker=ticker, ratio=ratio_name, period=period, limit=limit)


# ── docstring parsing ────────────────────────────────────────────────────────

def test_description_is_the_prose_before_args():
    description, _ = parse_docstring(sample)
    assert description.startswith("Calculate something for a company.")
    assert "second line of prose" in description


def test_returns_section_is_not_part_of_the_description():
    """It documents the shape for a human; putting it in the description spends
    the model's context on something the schema already conveys."""
    description, _ = parse_docstring(sample)
    assert "should not appear" not in description


def test_parameter_descriptions_are_extracted():
    _, params = parse_docstring(sample)
    assert params["ticker"].startswith("Stock ticker symbol")


def test_continuation_lines_are_joined():
    _, params = parse_docstring(sample)
    assert params["period"] == (
        'Which reporting period to use. One of "annual" or "quarterly", '
        "continuing onto a second line."
    )


# ── type mapping ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "annotation,expected",
    [
        (str, {"type": "string"}),
        (int, {"type": "integer"}),
        (float, {"type": "number"}),
        (bool, {"type": "boolean"}),
        (str | None, {"type": "string"}),
        (list[str], {"type": "array", "items": {"type": "string"}}),
        (dict, {"type": "object"}),
    ],
)
def test_json_schema_for_common_annotations(annotation, expected):
    assert json_schema_for(annotation) == expected


def test_literal_becomes_an_enum():
    """The most useful thing a schema carries: it stops a model inventing
    period="monthly"."""
    assert json_schema_for(Literal["annual", "quarterly"]) == {
        "type": "string", "enum": ["annual", "quarterly"]
    }


def test_an_unknown_annotation_falls_back_to_string():
    class Exotic:
        pass

    assert json_schema_for(Exotic) == {"type": "string"}


# ── spec construction ────────────────────────────────────────────────────────

def test_schema_shape_matches_the_openai_tools_format():
    schema = build_spec(sample).schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "sample"
    assert schema["function"]["parameters"]["type"] == "object"


def test_only_parameters_without_defaults_are_required():
    spec = build_spec(sample)
    assert set(spec.parameters["required"]) == {"ticker", "ratio_name"}


def test_defaults_are_published_in_the_schema():
    properties = build_spec(sample).parameters["properties"]
    assert properties["period"]["default"] == "annual"
    assert properties["limit"]["default"] == 5


def test_a_none_default_is_not_published_as_a_default():
    """`"default": null` tells a model to send null rather than omit it."""
    assert "default" not in build_spec(sample).parameters["properties"]["company"]


def test_hidden_parameters_are_absent_from_the_schema():
    def with_connection(query: str, conn=None) -> dict:
        """Do a thing.

        Args:
            query: what to look for.
        """
        return ok()

    spec = build_spec(with_connection, hidden=("conn",))
    assert "conn" not in spec.parameters["properties"]
    assert "conn" not in spec.accepted


def test_every_real_tool_produces_a_complete_schema():
    """Guards the actual contract: each advertised tool must reach the model
    with a description and a description for every argument."""
    from agent.tools import build_registry

    for schema in build_registry().schemas():
        function = schema["function"]
        assert function["description"], function["name"]
        for name, prop in function["parameters"]["properties"].items():
            assert prop.get("description"), f"{function['name']}.{name} has no description"


# ── argument coercion ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value,declared,expected",
    [
        ("3", "integer", 3),          # what a live model actually sent for k
        ("30", "integer", 30),
        ("3.0", "integer", 3),
        (3.0, "integer", 3),
        ("0.5", "number", 0.5),
        ("true", "boolean", True),
        ("False", "boolean", False),
        (2026, "string", "2026"),
    ],
)
def test_model_shaped_arguments_are_coerced(value, declared, expected):
    """Models emit JSON-ish, not JSON. Every one of these is a TypeError inside
    a tool that annotated the parameter honestly."""
    assert coerce(value, {"type": declared}) == expected


@pytest.mark.parametrize(
    "value,declared",
    [
        ("7d", "integer"),            # not a number at all
        ("maybe", "boolean"),
        ("NVDA", "string"),
        (None, "integer"),
        ([1, 2], "array"),
    ],
)
def test_uncoercible_values_pass_through_untouched(value, declared):
    """The tool's own validation writes a better message than a coercion guess."""
    assert coerce(value, {"type": declared}) == value


def test_a_bool_is_not_coerced_into_an_integer():
    """True == 1 in Python, so an unguarded int() would silently accept it."""
    assert coerce(True, {"type": "integer"}) is True


def test_coercion_reaches_the_tool(registry):
    """The end of the bug: a live model sent k="3" and search_filings raised
    TypeError, which the model reported to the user as 'the filing does not
    discuss this'."""
    result = run(registry.call(
        "sample", {"ticker": "NVDA", "ratio_name": "pe_ratio", "limit": "7"}
    ))
    assert result.ok is True
    assert result.result["limit"] == 7


# ── dispatch ─────────────────────────────────────────────────────────────────

@pytest.fixture
def registry():
    reg = Registry()
    reg.register(sample)
    return reg


def run(coro):
    return asyncio.run(coro)


def test_a_tool_is_called_with_its_arguments(registry):
    result = run(registry.call("sample", {"ticker": "NVDA", "ratio_name": "pe_ratio"}))
    assert result.ok is True
    assert result.result["ticker"] == "NVDA"
    assert result.latency_ms >= 0


def test_an_unknown_tool_lists_the_available_ones(registry):
    result = run(registry.call("get_weather", {}))
    assert result.result["error"] == ERROR_UNSUPPORTED
    assert "sample" in result.result["message"]


def test_missing_required_arguments_are_rejected(registry):
    result = run(registry.call("sample", {"ticker": "NVDA"}))
    assert result.result["error"] == ERROR_BAD_INPUT
    assert "ratio_name" in result.result["message"]


def test_unknown_arguments_are_dropped_not_rejected(registry):
    """Models invent plausible extras. Inside a five-iteration budget, spending
    a turn on 'you passed an argument I do not accept' is worse than ignoring
    it."""
    result = run(registry.call(
        "sample", {"ticker": "NVDA", "ratio_name": "pe_ratio", "as_of": "2026-01-01"}
    ))
    assert result.ok is True
    assert result.result["ignored_arguments"] == ["as_of"]


def test_non_dict_arguments_do_not_crash(registry):
    assert run(registry.call("sample", None)).result["error"] == ERROR_BAD_INPUT


def test_a_tool_that_raises_becomes_an_error_not_an_exception(registry):
    """An exception crossing this boundary aborts the agent's whole turn."""
    def explodes() -> dict:
        """Always fails."""
        raise RuntimeError("upstream on fire")

    registry.register(explodes)
    result = run(registry.call("explodes", {}))

    assert result.ok is False
    assert result.result["error"] == ERROR_UPSTREAM
    assert "upstream on fire" in result.result["message"]


def test_a_tool_returning_a_non_dict_is_an_error(registry):
    def wrong() -> dict:
        """Returns the wrong thing."""
        return "just a string"

    registry.register(wrong)
    assert run(registry.call("wrong", {})).result["error"] == ERROR_UPSTREAM


def test_async_tools_are_awaited(registry):
    async def fetch(name: str) -> dict:
        """Fetch a thing.

        Args:
            name: what to fetch.
        """
        await asyncio.sleep(0)
        return ok(fetched=name)

    registry.register(fetch)
    assert run(registry.call("fetch", {"name": "x"})).result["fetched"] == "x"


def test_sync_tools_run_off_the_event_loop(registry):
    """A blocking tool called directly would stall every other request this
    process is serving."""
    import threading

    seen: dict = {}

    def blocking() -> dict:
        """Blocks."""
        seen["thread"] = threading.current_thread().name
        return ok()

    registry.register(blocking)

    async def go():
        seen["loop_thread"] = threading.current_thread().name
        return await registry.call("blocking", {})

    run(go())
    assert seen["thread"] != seen["loop_thread"]


def test_registry_reports_its_contents(registry):
    assert "sample" in registry
    assert len(registry) == 1
    assert registry.get("sample") is not None
    assert registry.get("nope") is None


# ── annotations resolved to real types ──────────────────────────────────────
#
# Every tool module uses `from __future__ import annotations`, so
# `inspect.signature(...).annotation` is the *string* "int". Looking that up in
# _JSON_TYPES missed, fell back to "string", and so every parameter of every
# tool was declared a string -- which silently disabled coerce() and dropped
# the Literal enums. The model then called search_filings with k="1", nothing
# coerced it, and the tool raised
# "'<' not supported between instances of 'int' and 'str'".

def test_an_int_parameter_is_typed_integer_not_string():
    """The bug: `from __future__ import annotations` made this "string"."""
    from agent.tools import build_registry

    schema = next(s["function"] for s in build_registry().schemas()
                  if s["function"]["name"] == "search_filings")
    assert schema["parameters"]["properties"]["k"]["type"] == "integer"


def test_every_int_annotated_parameter_is_declared_integer():
    """Audited across all four tools, not just the one that crashed."""
    import inspect
    import typing

    from agent.tools import build_registry

    registry = build_registry()
    for spec_schema in registry.schemas():
        name = spec_schema["function"]["name"]
        fn = registry._tools[name].fn
        hints = typing.get_type_hints(fn)
        properties = spec_schema["function"]["parameters"]["properties"]
        for param in inspect.signature(fn).parameters.values():
            if param.name not in properties:
                continue
            if hints.get(param.name) is int:
                assert properties[param.name]["type"] == "integer", (
                    f"{name}.{param.name} is annotated int but declared "
                    f"{properties[param.name]['type']}"
                )


def test_literal_parameters_regain_their_enum():
    """The same root cause threw these away. An enum is the single most useful
    thing the schema carries -- it stops the model inventing period='monthly'."""
    from agent.tools import build_registry

    by_name = {s["function"]["name"]: s["function"] for s in build_registry().schemas()}
    assert by_name["calculate_ratio"]["parameters"]["properties"]["period"]["enum"] == [
        "annual", "quarterly", "ttm"
    ]
    assert "gross_margin" in (
        by_name["calculate_ratio"]["parameters"]["properties"]["ratio_name"]["enum"]
    )
    assert by_name["search_filings"]["parameters"]["properties"]["section"]["enum"] == [
        "risk", "mdna", "income"
    ]


def test_an_unresolvable_annotation_does_not_break_registration():
    """A hint naming something not importable at runtime must degrade to the
    old fallback, not take the whole registry down with it."""
    from tools.registry import build_spec

    def tool(value: "NotAnActualType") -> dict:  # noqa: F821
        """Does a thing.

        Args:
            value: some value.
        """
        return {"ok": True}

    spec = build_spec(tool)
    assert spec.parameters["properties"]["value"]["type"] == "string"


def test_search_filings_accepts_k_as_a_string():
    """The exact call from Run A of the Milestone 8 eval:
    search_filings({"k": "1", "query": "net income", "company": "NVIDIA"}).

    It raised a TypeError, the agent read that as a dead end, and it
    fabricated a share count rather than saying it could not answer."""
    import asyncio

    from agent.tools import build_registry

    async def go():
        registry = build_registry()
        return await registry.call(
            "search_filings", {"k": "1", "query": "net income", "company": "NVIDIA"}
        )

    from db.base import engine

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(go())
    except Exception as exc:  # noqa: BLE001 - no Postgres is a skip
        pytest.skip(f"corpus unavailable: {type(exc).__name__}: {exc}")
    finally:
        # Dispose on this loop before closing it. db.base pools connections
        # bound to the loop that opened them, so leaving them behind makes
        # every later DB test in the same process fail on a closed loop --
        # which tests/test_retrieval.py then reports as "Postgres
        # unreachable" and skips. Seventeen tests went quiet that way.
        loop.run_until_complete(engine.dispose())
        loop.close()

    assert result.ok, f"k as a string still fails: {result.result}"
    assert result.arguments["k"] == "1", "the raw argument is recorded as sent"
