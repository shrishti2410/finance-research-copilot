"""What the model is shown of a retrieved filing passage: prose is cut, a table never is.

Eval run-b (2026-11-10) lost five cases to one mechanism: `search_filings`
returned the income statement at rank 1 and cut its excerpt at 700
characters, which for a table chunk is the generated caption and nothing else.
Every expected figure sat past the cut (NVDA revenue 215,938 at char 713,
diluted shares 24,514 at char 3,017; AAPL Services 109,158 at 1,421). The
model said, accurately, that the excerpt listed the line items and not the
numbers.

These tests pin the fix: a table hit is returned whole with its parsed rows in
the full result, the model's compacted message keeps the whole excerpt and
drops the rows, and a prose cut lands on a sentence end. The last two tests run
against the real index and are skipped where it is not reachable.
"""

from __future__ import annotations

import asyncio

import pytest

from agent import tools
from agent.grounding import unsupported_figures
from agent.orchestrator import Step
from agent.tools import _excerpt, _rows, compact_for_model
from rag import vector_store as vs

# One event loop for the module, as tests/test_retrieval.py does. `asyncio.run`
# makes a fresh loop per call and the shared engine's pool stays bound to the
# first one; the second call then fails inside asyncpg with a message about a
# closed transport, which is a test bug and not an unavailable index.
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def close_loop():
    yield
    from db.base import engine
    _LOOP.run_until_complete(engine.dispose())
    _LOOP.close()

# ── the cut itself ────────────────────────────────────────────────────────────

PROSE = ("Revenue for fiscal year 2026 was $215.9 billion, up 65% from a year ago. "
         "Data Center revenue for fiscal year 2026 was up 68% from a year ago. "
         "The strong year-on-year growth was driven by demand for accelerated "
         "computing and AI solutions across hyperscale and enterprise customers. ") * 6


def test_a_table_is_never_cut():
    table = "Consolidated Statements of Income\n(In millions)\n" + "Revenue 215,938\n" * 200
    assert len(table) > tools.EXCERPT_CHARS
    assert _excerpt(table, "table") == table


def test_prose_within_the_limit_is_untouched():
    assert _excerpt("Short passage.", "text") == "Short passage."


def test_prose_is_cut_at_a_sentence_end_and_marked():
    cut = _excerpt(PROSE, "text")
    assert len(cut) <= tools.EXCERPT_CHARS + len(tools._CUT_MARKER)
    body = cut[: -len(tools._CUT_MARKER)]
    assert cut.endswith(tools._CUT_MARKER)
    assert body.endswith("."), f"cut mid-sentence: {body[-40:]!r}"
    assert body in PROSE


def test_prose_without_a_usable_sentence_end_is_cut_at_the_limit_not_beyond():
    run_on = "x" * 2000
    cut = _excerpt(run_on, "text")
    assert cut == "x" * tools.EXCERPT_CHARS + tools._CUT_MARKER


# ── the rows that ride in the full result ─────────────────────────────────────

STRUCTURED = {
    "units": "In millions, except per share data",
    "periods": ["Jan 25, 2026", "Jan 26, 2025"],
    "rows": [
        {"label": "Revenue", "kind": "amount", "values": [215938.0, 130497.0],
         "values_absolute": [215938000000.0, 130497000000.0]},
        {"label": "Net income per share:", "kind": "heading", "values": [],
         "values_absolute": []},
        {"label": "Diluted", "kind": "share_count", "values": [24514.0, 24804.0],
         "values_absolute": [24514000000.0, 24804000000.0]},
    ],
}


def test_rows_keep_label_kind_values_and_absolute_values_and_drop_headings():
    rows = _rows(STRUCTURED)
    assert [r["label"] for r in rows] == ["Revenue", "Diluted"]
    assert rows[1] == {"label": "Diluted", "kind": "share_count",
                       "values": [24514.0, 24804.0],
                       "values_absolute": [24514000000.0, 24804000000.0]}


def test_rows_of_nothing_is_an_empty_list():
    assert _rows(None) == [] and _rows({}) == []


def test_the_model_sees_the_whole_table_but_not_the_rows_json():
    table = "Consolidated Statements of Income\n(In millions)\n" + "Revenue 215,938\n" * 200
    full = {"ok": True, "count": 1, "results": [{
        "ticker": "NVDA", "fiscal_period": "FY2026", "section": "Income Statement",
        "content_type": "table", "score": 0.77, "excerpt": table,
        "source_url": "https://www.sec.gov/...", "units": STRUCTURED["units"],
        "periods": STRUCTURED["periods"], "rows": _rows(STRUCTURED),
    }]}
    shown = compact_for_model("search_filings", full)["results"][0]
    assert shown["excerpt"] == table
    assert shown["content_type"] == "table"
    assert "rows" not in shown and "units" not in shown and "source_url" not in shown


# ── against the real index ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def nvda_statement() -> dict:
    """The indexed NVDA income statement, searched once for the module.

    Skips only for the reasons the rest of the suite skips -- no encoder, no
    Postgres, no index. Anything else is a failure, not an absence.
    """
    try:
        from rag.embeddings import get_encoder
        get_encoder()
    except Exception as exc:  # noqa: BLE001 - weights not downloadable here
        pytest.skip(f"bge-small unavailable: {type(exc).__name__}")
    try:
        result = run(tools.search_filings(
            "consolidated income statement", company="NVDA", section="income", k=1))
    except vs.VectorStoreNotReady as exc:
        pytest.skip(str(exc))
    except (OSError, ConnectionError) as exc:
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")
    hits = [h for h in result["results"] if h["content_type"] == "table"]
    if not hits:
        pytest.skip("NVDA FY2026 is not indexed here")
    return hits[0]


def test_the_indexed_statement_arrives_whole_with_its_figures(nvda_statement):
    hit = nvda_statement
    excerpt = hit["excerpt"]
    assert len(excerpt) > tools.EXCERPT_CHARS, "the statement is longer than the prose cut"
    for figure in ("215,938", "4.90", "24,514"):
        assert figure in excerpt, f"{figure} missing from the excerpt the model sees"
    shares = [r for r in hit["rows"] if r["kind"] == "share_count"]
    assert shares, "share-count rows must be parsed"
    assert any(abs(v - 24514e6) < 1e6 for r in shares for v in r["values_absolute"]), \
        "the diluted share count must be in the trace in absolute units"


def test_market_cap_from_the_real_statement_and_a_price_is_grounded(nvda_statement):
    """End to end with the guard: the number the eval's market-cap cases need."""
    hit = nvda_statement
    steps = [
        Step(iteration=1, tool="get_stock_price", arguments={},
             result={"ok": True, "ticker": "NVDA", "last": {"date": "2026-08-07", "close": 223.71}},
             latency_ms=1, model_latency_ms=1, ok=True),
        Step(iteration=2, tool="search_filings", arguments={},
             result={"ok": True, "count": 1, "results": [hit]},
             latency_ms=1, model_latency_ms=1, ok=True),
    ]
    answer = ("With 24,514 million diluted shares and a close of $223.71, the implied "
              "market capitalisation is about $5.48 trillion.")
    assert [f.text for f in unsupported_figures(answer, steps)] == []
