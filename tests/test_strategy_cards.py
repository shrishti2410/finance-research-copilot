"""The strategy cards: format, coverage of the code, and retrieval.

The format and coverage tests need nothing. The index tests run against the
Postgres in DATABASE_URL (migrated to 0007) inside a transaction that is
rolled back, so the real collection is never touched; they skip without
Postgres, and the retrieval test also skips without the bge-small weights.
"""

from __future__ import annotations

import asyncio
import hashlib

import numpy as np
import pytest
from sqlalchemy import text

from market.strategies import STRATEGIES
from rag.strategy_cards import (
    REQUIRED_SECTIONS,
    CardError,
    index_cards,
    load_cards,
    parse_card,
    search_cards,
)

OPTION_CARDS = {"long_call", "long_put", "covered_call", "protective_put",
                "bull_call_spread", "bear_put_spread", "long_straddle", "iron_condor"}

CARDS = load_cards()


def test_there_are_25_to_30_cards_with_unique_keys():
    keys = [c.key for c in CARDS]
    assert 25 <= len(CARDS) <= 30
    assert len(set(keys)) == len(keys)


def test_every_strategy_in_the_code_has_a_card_that_links_to_it():
    equity = {c.key: c for c in CARDS if c.kind == "equity"}
    assert set(STRATEGIES) <= set(equity), set(STRATEGIES) - set(equity)
    for key, card in equity.items():
        path, _, name = card.code.partition("#")
        assert path == "market/strategies.py" and name in STRATEGIES, card.code
        assert f"`{name}`" in card.sections["Code"]


def test_the_eight_options_cards_exist_and_state_their_view_and_risk():
    options = {c.key: c for c in CARDS if c.kind == "option"}
    assert set(options) == OPTION_CARDS
    for card in options.values():
        assert card.view, card.key
        risk = card.sections["Risk"].lower()
        assert "maximum loss" in risk and "breakeven" in risk, card.key


def test_every_card_has_every_section():
    for card in CARDS:
        assert all(card.sections.get(s) for s in REQUIRED_SECTIONS), card.key


def test_the_long_short_card_says_shorts_are_not_available_in_india():
    card = next(c for c in CARDS if c.key == "sma_cross_long_short")
    assert "cannot be held short overnight" in card.sections["Risk"]


GOOD = """---
key: demo
name: Demo
kind: equity
regime: trending
code: market/strategies.py#sma_cross
---
# Demo
""" + "".join(f"## {s}\nText.\n\n" for s in REQUIRED_SECTIONS)


def test_a_card_missing_a_section_or_with_a_bad_kind_is_refused():
    assert parse_card(GOOD).key == "demo"
    with pytest.raises(CardError, match="When it fails"):
        parse_card(GOOD.replace("## When it fails\nText.", "## When it fails\n"))
    with pytest.raises(CardError, match="kind"):
        parse_card(GOOD.replace("kind: equity", "kind: crypto"))
    with pytest.raises(CardError, match="regime"):
        parse_card(GOOD.replace("regime: trending", "regime: bullish"))
    with pytest.raises(CardError, match="front matter"):
        parse_card(GOOD.split("---\n", 2)[2])


# --- the collection in Postgres ----------------------------------------------

def _fake_embed(texts):
    """Deterministic unit vectors from a hash: enough to test the SQL."""
    out = []
    for t in texts:
        seed = int(hashlib.sha256(t.encode()).hexdigest()[:8], 16)
        v = np.random.default_rng(seed).normal(size=384)
        out.append(v / np.linalg.norm(v))
    return np.array(out, dtype=np.float32)


def _in_rolled_back_transaction(work):
    from db.base import engine

    async def go():
        try:
            async with engine.connect() as conn:
                tx = await conn.begin()
                try:
                    return await work(conn)
                finally:
                    await tx.rollback()
        finally:
            await engine.dispose()

    try:
        return asyncio.run(go())
    except Exception as exc:  # noqa: BLE001
        if "strategy_cards" in str(exc) or type(exc).__name__ in (
                "OSError", "ConnectionRefusedError", "InvalidPasswordError"):
            pytest.skip(f"strategy_cards not available ({type(exc).__name__}); migrate to 0007")
        raise


def test_indexing_replaces_the_collection_and_filters_apply_before_the_top_k():
    async def work(conn):
        written = await index_cards(conn, CARDS, embed=_fake_embed)
        count = (await conn.execute(text("SELECT count(*) FROM strategy_cards"))).scalar()
        options = await search_cards(conn, "anything", k=500, kind="option",
                                     embed=lambda q: _fake_embed([q])[0])
        one = await search_cards(conn, "anything", k=3, card="iron_condor", section="Risk",
                                 embed=lambda q: _fake_embed([q])[0])
        # Indexing again leaves the same number of rows: a replace, not an append.
        again = await index_cards(conn, CARDS[:3], embed=_fake_embed)
        after = (await conn.execute(text("SELECT count(*) FROM strategy_cards"))).scalar()
        return written, count, options, one, again, after

    written, count, options, one, again, after = _in_rolled_back_transaction(work)
    assert written == count == sum(len(c.sections) for c in CARDS)
    assert options and {h.kind for h in options} == {"option"}
    assert len(options) == sum(len(c.sections) for c in CARDS if c.kind == "option")
    assert [(h.card_key, h.section) for h in one] == [("iron_condor", "Risk")]
    assert one[0].source == "docs/strategies/iron_condor.md"
    assert after == again == sum(len(c.sections) for c in CARDS[:3])


# Each question, and the card it should find. Measured on 2026-11-16 with
# bge-small: the first five rank the expected card first; the straddle
# question ranks it second, behind rsi2_pullback.
PROBES = [
    ("why does the golden cross lose money in a sideways market", "sma_cross", 1),
    ("insurance for shares I already own", "protective_put", 1),
    ("can I short an Indian stock overnight", "sma_cross_long_short", 1),
    ("strategy that buys oversold stocks", "rsi_reversion", 1),
    ("what is the maximum loss of an iron condor", "iron_condor", 1),
    ("profit from a big move after results without knowing the direction",
     "long_straddle", 3),
]


def test_real_questions_find_the_right_card():
    try:
        from rag.embeddings import embed_query
        embed_query("warm-up")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"bge-small unavailable: {type(exc).__name__}")

    async def work(conn):
        await index_cards(conn, CARDS)
        return {q: [h.card_key for h in await search_cards(conn, q, k=3)]
                for q, _, _ in PROBES}

    found = _in_rolled_back_transaction(work)
    for question, expected, within in PROBES:
        assert expected in found[question][:within], (question, found[question])
