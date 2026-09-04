"""Tests for the embedding wrapper.

Everything here needs the real bge-small weights, so the whole module skips
cleanly when they are not present rather than asserting against a stub -- the
properties worth testing (unit norm, query/passage asymmetry, window pooling)
are properties of the actual model, and a mock would only test the mock.
"""

import numpy as np
import pytest

from rag.embeddings import (
    EMBEDDING_DIM,
    LONG_INPUT_OVERLAP,
    MAX_SEQUENCE_TOKENS,
    QUERY_PREFIX,
    _windows,
    embed_passages,
    embed_passages_timed,
    embed_query,
    get_encoder,
    input_token_lengths,
)


@pytest.fixture(scope="module", autouse=True)
def encoder_available():
    try:
        get_encoder()
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot run these"
        pytest.skip(f"bge-small unavailable: {type(exc).__name__}: {exc}")


SHORT = "Competition could adversely impact our market share and financial results."


def long_text(words: int = 900) -> str:
    return " ".join(
        f"Risk factor sentence number {i} concerns supply chain concentration."
        for i in range(words // 8)
    )


# ── shape and normalization ──────────────────────────────────────────────────

def test_passage_vectors_have_the_declared_dimension():
    assert embed_passages([SHORT, "Another passage."]).shape == (2, EMBEDDING_DIM)


def test_query_vector_has_the_declared_dimension():
    assert embed_query("what are the risks?").shape == (EMBEDDING_DIM,)


def test_vectors_are_unit_length():
    """vector_store computes cosine as a bare dot product. If vectors are not
    normalized, every score it reports is wrong."""
    vectors = embed_passages([SHORT, long_text(), "x"])
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)
    assert np.isclose(np.linalg.norm(embed_query(SHORT)), 1.0, atol=1e-5)


def test_empty_input_returns_an_empty_matrix_not_an_error():
    assert embed_passages([]).shape == (0, EMBEDDING_DIM)


def test_encoding_is_deterministic():
    assert np.array_equal(embed_passages([SHORT]), embed_passages([SHORT]))


def test_batching_does_not_change_vectors():
    """Padding differs between batch sizes; the vectors must not."""
    texts = [SHORT, "Second.", "Third passage here.", long_text(200)]
    assert np.allclose(
        embed_passages(texts, batch_size=1), embed_passages(texts, batch_size=4), atol=1e-5
    )


# ── query / passage asymmetry ────────────────────────────────────────────────

def test_query_prefix_is_applied_to_queries_only():
    """BGE is asymmetric. Silently embedding a query as a passage costs recall
    without erroring, so the difference is asserted rather than assumed."""
    as_query = embed_query(SHORT)
    as_passage = embed_passages([SHORT])[0]
    assert not np.allclose(as_query, as_passage, atol=1e-4)
    # ...and the query path really is "prefix + text" through the passage path.
    assert np.allclose(as_query, embed_passages([QUERY_PREFIX + SHORT])[0], atol=1e-5)


def test_related_text_scores_above_unrelated_text():
    """The one end-to-end sanity check: the model must actually rank."""
    query = embed_query("What are the competitive risks?")
    related = embed_passages(["We face intense competition from other chipmakers."])[0]
    unrelated = embed_passages(["The cafeteria menu changes on Tuesdays."])[0]
    assert float(query @ related) > float(query @ unrelated)


# ── long inputs ──────────────────────────────────────────────────────────────

def test_short_text_is_a_single_window():
    assert _windows(SHORT, "BAAI/bge-small-en-v1.5") == [SHORT]


def test_long_text_is_split_into_overlapping_windows():
    windows = _windows(long_text(), "BAAI/bge-small-en-v1.5")
    assert len(windows) > 1
    lengths = input_token_lengths(windows)
    assert all(n <= MAX_SEQUENCE_TOKENS for n in lengths), lengths


def test_long_text_embedding_reflects_its_tail():
    """The whole point of windowing. With plain truncation these two -- same
    512-token head, different tails -- would embed identically."""
    head = long_text()
    tail_a = head + " Our exposure is concentrated in semiconductor fabrication."
    tail_b = head + " Our exposure is concentrated in retail store leases."

    assert input_token_lengths([tail_a])[0] > MAX_SEQUENCE_TOKENS
    a, b = embed_passages([tail_a, tail_b])
    assert not np.allclose(a, b, atol=1e-4)


def test_windowed_vectors_are_still_unit_length():
    """A mean of unit vectors is not a unit vector; it has to be renormalized."""
    vector = embed_passages([long_text()])[0]
    assert np.isclose(np.linalg.norm(vector), 1.0, atol=1e-5)


def test_window_overlap_is_smaller_than_the_window():
    """A stride of zero or less would loop forever building windows."""
    assert 0 <= LONG_INPUT_OVERLAP < MAX_SEQUENCE_TOKENS - 2


# ── instrumentation ──────────────────────────────────────────────────────────

def test_timing_reports_counts_and_windowing():
    texts = [SHORT, long_text()]
    vectors, timing = embed_passages_timed(texts)

    assert vectors.shape == (2, EMBEDDING_DIM)
    assert timing.count == 2
    assert timing.seconds > 0
    assert timing.windowed == 1
    assert timing.max_input_tokens > MAX_SEQUENCE_TOKENS
    assert timing.per_item_ms > 0 and timing.per_second > 0
