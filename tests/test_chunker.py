"""Tests for the chunker.

Tests that need real token counts load the Qwen2.5-1.5B-Instruct tokenizer and
skip cleanly when it is not available (no transformers, or no local weights) --
so the suite still runs on a machine that has not downloaded it. The pure-text
logic (sentence splitting, heading detection, table rendering) is tested
unconditionally.
"""

from datetime import date

import pytest

from ingestion.chunker import (
    OVERLAP_TOKENS,
    TARGET_TOKENS,
    Chunk,
    ChunkMetadata,
    _is_heading,
    _Unit,
    chunk_filing,
    count_tokens,
    render_table,
    split_sentences,
)
from ingestion.edgar_client import Filing
from ingestion.parser import FinancialTable, ParsedFiling, Section, TableRow


@pytest.fixture(scope="module")
def tokenizer_available():
    try:
        count_tokens("probe")
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot run these"
        pytest.skip(f"Qwen tokenizer unavailable: {type(exc).__name__}")
    return True


FILING = Filing(
    ticker="NVDA",
    company_name="NVIDIA CORP",
    cik="0001045810",
    form="10-K",
    accession="0001045810-26-000021",
    filing_date=date(2026, 2, 25),
    report_date=date(2026, 1, 25),
    primary_document="nvda-20260125.htm",
    document_url="https://www.sec.gov/Archives/edgar/data/1045810/x/nvda-20260125.htm",
)

TABLE = FinancialTable(
    title="Consolidated Statements of Income",
    units="In millions, except per share data",
    periods=["Jan 25, 2026", "Jan 26, 2025"],
    rows=[
        TableRow(label="Revenue", values=[215938.0, 130497.0]),
        TableRow(label="Cost of revenue", values=[62475.0, 32639.0]),
        TableRow(label="Gross profit", values=[153463.0, 97858.0]),
        TableRow(label="Net income per share:", values=[]),
        TableRow(label="Diluted", values=[4.90, 2.94]),
        TableRow(label="Weighted average shares used in per share computation:", values=[]),
    ],
    row_count=6,
    numeric_cell_count=8,
    match_score=7,
)


def make_section(item: str, text: str, title: str = "Risk Factors") -> Section:
    return Section(
        item=item, title=title, heading=f"Item {item}. {title}",
        text=text, char_count=len(text), block_count=text.count("\n\n") + 1,
    )


def long_prose(paragraphs: int = 40) -> str:
    """Filler with distinguishable paragraphs, each a handful of sentences."""
    return "\n\n".join(
        f"Paragraph {i} begins here and discusses risk number {i} at some length. "
        f"It continues with a second sentence about supply chains and demand. "
        f"A third sentence closes the thought about paragraph {i}."
        for i in range(paragraphs)
    )


# ── sentence splitting ───────────────────────────────────────────────────────

def test_split_sentences_basic():
    assert split_sentences("One thing happened. Another followed.") == [
        "One thing happened.",
        "Another followed.",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "We sell in the U.S. Securities markets remain volatile.",
        "Our subsidiary Acme Inc. filed a report.",
        "Revenue rose to $1.5 billion in 2025.",
    ],
)
def test_split_sentences_does_not_break_on_abbreviations_or_decimals(text):
    """Splitting inside 'U.S.' or '$1.5' produces fragments that read as
    truncated mid-thought."""
    parts = split_sentences(text)
    assert all(len(p.split()) > 2 for p in parts), parts


def test_split_sentences_rejoins_lowercase_continuations():
    assert split_sentences("Ends here. and continues lowercase.") == [
        "Ends here. and continues lowercase."
    ]


# ── heading detection ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,tokens,expected",
    [
        ("Risks Related to Our Industry", 6, True),
        ("This is a full sentence.", 6, False),      # terminal punctuation
        ("•A bullet without punctuation", 6, False),  # bullets are content
        ("A very long line that is not a heading at all because it runs on", 40, False),
    ],
)
def test_is_heading(text, tokens, expected):
    assert _is_heading(_Unit(text, tokens, 0)) is expected


# ── table rendering ──────────────────────────────────────────────────────────

def test_render_table_keeps_labels_and_values_on_one_line():
    rendered = render_table(TABLE)
    revenue = next(line for line in rendered.splitlines() if line.startswith("Revenue"))
    assert "215,938" in revenue and "130,497" in revenue


def test_render_table_does_not_truncate_long_labels():
    """A clipped label ('...per share computatio') would be embedded as-is."""
    assert "Weighted average shares used in per share computation:" in render_table(TABLE)


def test_render_table_separates_period_headers():
    """Narrow columns ran headers together: 'September 27,2025September 28,2024'."""
    header = next(line for line in render_table(TABLE).splitlines() if "Jan 25, 2026" in line)
    assert "Jan 25, 2026" in header and "Jan 26, 2025" in header
    assert "2026Jan" not in header


def test_render_table_keeps_decimals_for_per_share_values():
    assert "4.90" in render_table(TABLE)


def test_render_table_shows_grouping_rows_without_numbers():
    assert "Net income per share:" in render_table(TABLE)


# ── chunking ─────────────────────────────────────────────────────────────────

def test_table_is_one_chunk_and_never_split(tokenizer_available):
    parsed = ParsedFiling(risk_factors=None, mdna=None, income_statement=TABLE)
    chunks = chunk_filing(parsed, FILING)

    tables = [c for c in chunks if c.metadata.content_type == "table"]
    assert len(tables) == 1
    # Splitting a statement would sever line items from their column headers.
    assert "Revenue" in tables[0].content and "Gross profit" in tables[0].content
    assert tables[0].structured["periods"] == ["Jan 25, 2026", "Jan 26, 2025"]
    assert tables[0].structured["rows"][0]["values"] == [215938.0, 130497.0]


def test_every_chunk_carries_the_required_metadata(tokenizer_available):
    parsed = ParsedFiling(
        risk_factors=make_section("1A", long_prose()),
        mdna=make_section("7", long_prose(10), title="MD&A"),
        income_statement=TABLE,
    )
    chunks = chunk_filing(parsed, FILING)
    assert chunks

    for chunk in chunks:
        meta = chunk.metadata
        assert meta.company == "NVIDIA CORP"
        assert meta.filing_type == "10-K"
        assert meta.fiscal_period == "FY2026"
        assert meta.section
        assert isinstance(meta.chunk_index, int)


def test_chunk_index_is_contiguous_across_the_document(tokenizer_available):
    parsed = ParsedFiling(
        risk_factors=make_section("1A", long_prose()),
        mdna=make_section("7", long_prose(10), title="MD&A"),
        income_statement=TABLE,
    )
    chunks = chunk_filing(parsed, FILING)
    assert [c.metadata.chunk_index for c in chunks] == list(range(len(chunks)))


def test_fiscal_period_uses_period_end_not_filing_date(tokenizer_available):
    """NVDA files FY2026 in Feb 2026; AAPL files FY2025 in Oct 2025. The period
    end is what both call the fiscal year."""
    parsed = ParsedFiling(risk_factors=None, mdna=None, income_statement=TABLE)
    assert chunk_filing(parsed, FILING)[0].metadata.fiscal_period == "FY2026"


def test_chunks_respect_the_token_target(tokenizer_available):
    parsed = ParsedFiling(
        risk_factors=make_section("1A", long_prose(60)), mdna=None, income_statement=None
    )
    chunks = chunk_filing(parsed, FILING)
    assert len(chunks) > 1

    for chunk in chunks:
        # A small overshoot is expected: chunks are packed using per-unit counts,
        # but BPE merges across unit boundaries, so the re-encoded total can run
        # a few tokens over. Anything beyond a 10% margin is a real bug.
        assert chunk.token_count <= TARGET_TOKENS * 1.1, chunk.token_count


def test_consecutive_chunks_overlap(tokenizer_available):
    parsed = ParsedFiling(
        risk_factors=make_section("1A", long_prose(60)), mdna=None, income_statement=None
    )
    chunks = chunk_filing(parsed, FILING)
    overlapping = sum(
        1 for a, b in zip(chunks, chunks[1:]) if b.content[:80] and b.content[:80] in a.content
    )
    assert overlapping >= len(chunks) - 2


def test_chunks_end_on_sentence_boundaries(tokenizer_available):
    parsed = ParsedFiling(
        risk_factors=make_section("1A", long_prose(60)), mdna=None, income_statement=None
    )
    for chunk in chunk_filing(parsed, FILING):
        assert not chunk.hard_split
        assert chunk.content.rstrip().endswith((".", "!", "?", ":", ";"))


def test_oversized_sentence_is_hard_split_and_flagged(tokenizer_available):
    """The one case where a chunk may end mid-sentence must be visible, not silent."""
    monster = "word " * (TARGET_TOKENS * 2)
    parsed = ParsedFiling(
        risk_factors=make_section("1A", monster.strip() + "."), mdna=None, income_statement=None
    )
    chunks = chunk_filing(parsed, FILING)
    assert len(chunks) > 1
    assert any(c.hard_split for c in chunks)


def test_sections_do_not_bleed_into_each_other(tokenizer_available):
    parsed = ParsedFiling(
        risk_factors=make_section("1A", "Risk prose about supply chains."),
        mdna=make_section("7", "MD&A prose about revenue growth.", title="MD&A"),
        income_statement=None,
    )
    chunks = chunk_filing(parsed, FILING)
    risk = [c for c in chunks if c.metadata.section.startswith("Item 1A")]
    mdna = [c for c in chunks if c.metadata.section.startswith("Item 7")]
    assert len(risk) == 1 and len(mdna) == 1
    assert "MD&A prose" not in risk[0].content
    assert "Risk prose" not in mdna[0].content


def test_token_counts_are_real_not_word_counts(tokenizer_available):
    """Financial text tokenizes worse than prose; a word count would understate
    it and silently blow a context budget."""
    text = "Revenue of $215,938 million rose 62% year-over-year in FY2026."
    assert count_tokens(text) > len(text.split())


def test_metadata_round_trips_to_dict():
    meta = ChunkMetadata(
        company="NVIDIA CORP", filing_type="10-K", fiscal_period="FY2026",
        section="Item 1A - Risk Factors", chunk_index=3,
    )
    as_dict = meta.to_dict()
    assert as_dict["company"] == "NVIDIA CORP"
    assert as_dict["chunk_index"] == 3
    assert set(("company", "filing_type", "fiscal_period", "section", "chunk_index")) <= as_dict.keys()


def test_chunk_repr_is_useful():
    meta = ChunkMetadata(
        company="c", filing_type="10-K", fiscal_period="FY2026", section="Item 1A", chunk_index=2
    )
    assert "Item 1A" in repr(Chunk(chunk_id="x", content="y", metadata=meta, token_count=7))
