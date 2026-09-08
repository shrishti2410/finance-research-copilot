"""Tests for the retrieval-only harness.

The metric arithmetic is tested on synthetic outcomes -- no database, no
embedding model -- because that is the part that can silently report a wrong
number. Gold-set resolution is tested against the live corpus and skips when
Postgres is unreachable, matching the other DB-backed suites.
"""

import asyncio

import pytest

from eval.datasets import load_cases
from eval.retrieval_eval import RetrievalOutcome
from eval.retrieval_gold import MARKERS, resolve_gold


def outcome(gold, retrieved) -> RetrievalOutcome:
    return RetrievalOutcome(
        case_id="c", question="q", ticker="NVDA", marker="m",
        gold=frozenset(gold), retrieved=list(retrieved),
        scores=[0.0] * len(retrieved),
    )


# ── the metrics ─────────────────────────────────────────────────────────────

def test_recall_is_the_share_of_gold_chunks_returned():
    o = outcome({"a", "b", "c"}, ["a", "x", "b", "y", "z"])
    assert o.recall_at(3) == pytest.approx(2 / 3)
    assert o.recall_at(5) == pytest.approx(2 / 3)


def test_hit_is_true_when_any_gold_chunk_is_in_range():
    """A fact in three chunks is answerable from any one of them, which is why
    hit@k and recall@k are both reported."""
    o = outcome({"a", "b", "c"}, ["x", "y", "a", "z", "w"])
    assert o.hit_at(3) is True
    assert o.recall_at(3) == pytest.approx(1 / 3)


def test_a_gold_chunk_past_k_does_not_count():
    o = outcome({"a"}, ["x", "y", "z", "w", "a"])
    assert o.hit_at(3) is False
    assert o.hit_at(5) is True
    assert o.recall_at(3) == 0.0


def test_first_gold_rank_is_one_based():
    assert outcome({"b"}, ["a", "b", "c"]).first_gold_rank == 2
    assert outcome({"b"}, ["a", "c"]).first_gold_rank is None


def test_a_perfect_result_scores_one():
    o = outcome({"a", "b"}, ["a", "b", "c"])
    assert o.recall_at(3) == 1.0 and o.hit_at(3)


def test_an_empty_gold_set_scores_zero_rather_than_dividing_by_zero():
    assert outcome(set(), ["a"]).recall_at(3) == 0.0


def test_kind_splits_table_answers_from_prose_answers():
    table = outcome({"0001045810-26-000021:income_statement:71"}, [])
    prose = outcome({"0001045810-26-000021:7:63"}, [])
    assert table.kind == "table"
    assert prose.kind == "prose"


# ── the gold set ────────────────────────────────────────────────────────────

def test_every_filing_answerable_case_has_a_marker():
    """A case with no marker is silently excluded from the retrieval score,
    which quietly shrinks the eval."""
    need = {c.id for c in load_cases() if c.needs_filings}
    assert need == set(MARKERS), (
        f"missing markers: {sorted(need - set(MARKERS))}; "
        f"stale markers: {sorted(set(MARKERS) - need)}"
    )


def test_no_marker_names_a_chunk_id():
    """Markers must be content, not ids. An id-based gold set is the one you
    get by writing down what the retriever returned, which measures nothing."""
    for case_id, (_ticker, marker) in MARKERS.items():
        assert ":" not in marker, f"{case_id} marker looks like a chunk id"


def test_every_marker_resolves_against_the_live_corpus():
    from db.base import SessionLocal, engine

    async def go():
        async with SessionLocal() as session:
            return await resolve_gold(session)

    loop = asyncio.new_event_loop()
    try:
        gold = loop.run_until_complete(go())
        loop.run_until_complete(engine.dispose())
    except Exception as exc:  # noqa: BLE001 - Postgres down is a skip
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}: {exc}")
    finally:
        loop.close()

    unresolved = sorted(g.case_id for g in gold.values() if not g.ok)
    assert not unresolved, f"markers matching no chunk: {unresolved}"
