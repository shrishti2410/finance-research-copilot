"""Tests for the 10-K parser.

Most tests run against a small synthetic filing that reproduces the structure
that matters -- a table-of-contents table plus body-level heading divs -- so
they need no network and no 2 MB fixture. The tests at the bottom run against
real cached filings when `data/edgar/` has them, and skip otherwise.
"""

from pathlib import Path

import pytest

from ingestion.parser import (
    _parse_number,
    _stitch_fragments,
    find_boilerplate,
    find_item_markers,
    _body_blocks,
    _to_document,
    parse_10k,
)

CACHE = Path(__file__).resolve().parent.parent / "data" / "edgar"

# Mirrors real inline-XBRL filings: an XML declaration, a TOC laid out as a
# table, and the real section headings as body-level divs.
SYNTHETIC = """<?xml version='1.0' encoding='ASCII'?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div><table>
  <tr><td><div><span><a>Item 1A.</a></span></div></td><td>Risk Factors</td><td>5</td></tr>
  <tr><td><div><span><a>Item 7.</a></span></div></td><td>MD&amp;A</td><td>20</td></tr>
</table></div>
<div>Item 1. Business</div>
<div>We design things.</div>
<div>Item 1A. Risk Factors</div>
<div>Competition could harm us. Our margins may fall.</div>
<div>Table of Contents</div>
<div>12</div>
<div>Supply chains are exposed to acts of war or other military</div>
<div>actions, epidemics and other disruptions.</div>
<div>Item 1B. Unresolved Staff Comments</div>
<div>None.</div>
<div>Item 7. Management&#8217;s Discussion and Analysis of Financial Condition</div>
<div>Revenue grew because demand grew.</div>
<div>Item 7A. Quantitative and Qualitative Disclosures</div>
<div>Interest rate risk.</div>
<div>Item 8. Financial Statements and Supplementary Data</div>
<div>CONSOLIDATED STATEMENTS OF INCOME</div>
<div>(In millions, except per share data)</div>
<div><table>
  <tr><td></td><td>2026</td><td>2025</td><td>2024</td></tr>
  <tr><td>Revenue</td><td>$</td><td>215,938</td><td>130,497</td><td>60,922</td></tr>
  <tr><td>Cost of revenue</td><td></td><td>62,475</td><td>32,639</td><td>16,621</td></tr>
  <tr><td>Gross profit</td><td></td><td>153,463</td><td>97,858</td><td>44,301</td></tr>
  <tr><td>Research and development</td><td></td><td>18,497</td><td>12,914</td><td>8,675</td></tr>
  <tr><td>Operating income</td><td></td><td>130,387</td><td>81,453</td><td>32,972</td></tr>
  <tr><td>Other income, net</td><td></td><td>(259)</td><td>(247)</td><td>(257)</td></tr>
  <tr><td>Net income</td><td></td><td>120,067</td><td>72,880</td><td>29,760</td></tr>
</table></div>
<div>Item 9. Changes in and Disagreements with Accountants</div>
<div>None.</div>
</body></html>
"""


@pytest.fixture(scope="module")
def parsed():
    return parse_10k(SYNTHETIC)


@pytest.fixture(scope="module")
def blocks():
    return _body_blocks(_to_document(SYNTHETIC))


# ── heading location ─────────────────────────────────────────────────────────

def test_xml_declaration_does_not_break_parsing(parsed):
    """lxml refuses a str carrying an encoding declaration; every real filing
    has one."""
    assert parsed.risk_factors is not None


def test_table_of_contents_is_not_mistaken_for_a_section(blocks):
    """The TOC repeats every heading. Picking its copy yields a page-number list
    instead of the section, which is the classic failure of text-level search."""
    markers = find_item_markers(blocks)
    heading_block = blocks[markers["1A"]]
    assert not heading_block.xpath("ancestor::table")
    assert heading_block.text_content().strip() == "Item 1A. Risk Factors"


def test_every_expected_item_is_found_once(blocks):
    markers = find_item_markers(blocks)
    for item in ("1", "1A", "1B", "7", "7A", "8", "9"):
        assert item in markers, f"missing Item {item}"
    # Ordering must follow the document, not the ITEM_ORDER list.
    assert markers["1"] < markers["1A"] < markers["1B"] < markers["7"] < markers["8"]


def test_item_1_does_not_swallow_item_1a(blocks):
    markers = find_item_markers(blocks)
    assert markers["1"] != markers["1A"]


# ── section extraction ───────────────────────────────────────────────────────

def test_section_stops_at_the_next_item(parsed):
    assert "Competition could harm us." in parsed.risk_factors.text
    assert "Unresolved Staff Comments" not in parsed.risk_factors.text
    assert "Revenue grew" not in parsed.risk_factors.text


def test_mdna_extracted_separately(parsed):
    assert "Revenue grew because demand grew." in parsed.mdna.text
    assert "Interest rate risk" not in parsed.mdna.text


def test_page_furniture_is_removed(parsed):
    assert "Table of Contents" not in parsed.risk_factors.text
    assert "\n\n12\n\n" not in parsed.risk_factors.text
    assert parsed.risk_factors.boilerplate_blocks_dropped >= 2


def test_sentence_split_across_blocks_is_stitched(parsed):
    """Filings break a sentence over a page boundary. Removing the page number
    leaves two fragments that must be rejoined, or a chunk boundary can land
    inside the sentence."""
    assert "acts of war or other military actions, epidemics" in parsed.risk_factors.text


def test_stitching_leaves_headings_alone():
    # A heading has no terminal punctuation either, but what follows is
    # capitalised, so it must not be merged into the next paragraph.
    assert _stitch_fragments(["A Heading", "Body starts here."]) == [
        "A Heading",
        "Body starts here.",
    ]
    assert _stitch_fragments(["ends mid", "sentence here."]) == ["ends mid sentence here."]


def test_boilerplate_detector_finds_repeated_short_lines():
    class _Fake:
        def __init__(self, text):
            self._text = text

        def text_content(self):
            return self._text

    blocks = [_Fake("Table of Contents")] * 6 + [_Fake("unique prose line")]
    assert "Table of Contents" in find_boilerplate(blocks)
    assert "unique prose line" not in find_boilerplate(blocks)


# ── numbers ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "cell,expected",
    [
        ("215,938", 215938.0),
        ("$ 130,497", 130497.0),
        ("(1,234)", -1234.0),      # accounting negative
        ("4.93", 4.93),
        ("—", None),              # em dash means "nil"
        ("", None),
        ("Revenue", None),
        ("55.6 %", 55.6),
    ],
)
def test_parse_number(cell, expected):
    assert _parse_number(cell) == expected


# ── income statement ─────────────────────────────────────────────────────────

def test_income_statement_detected(parsed):
    table = parsed.income_statement
    assert table is not None
    assert table.periods == ["2026", "2025", "2024"]
    assert table.units == "In millions, except per share data"
    assert table.scale == 1_000_000


def test_income_statement_rows_keep_label_to_value_association(parsed):
    rows = {r.label: r.values for r in parsed.income_statement.rows}
    assert rows["Revenue"] == [215938.0, 130497.0, 60922.0]
    assert rows["Net income"] == [120067.0, 72880.0, 29760.0]
    # The '$' cell must not be mistaken for a value and shift the row.
    assert len(rows["Revenue"]) == 3


def test_income_statement_arithmetic_is_consistent(parsed):
    rows = {r.label: r.values for r in parsed.income_statement.rows}
    assert rows["Revenue"][0] - rows["Cost of revenue"][0] == rows["Gross profit"][0]


def test_parenthesised_values_become_negative(parsed):
    rows = {r.label: r.values for r in parsed.income_statement.rows}
    assert rows["Other income, net"] == [-259.0, -247.0, -257.0]


def test_segment_note_is_not_returned_as_the_income_statement():
    """Segment notes carry the same line items and would otherwise outrank the
    real statement."""
    doc = SYNTHETIC.replace(
        "CONSOLIDATED STATEMENTS OF INCOME", "Note 13 - Segment Information"
    )
    assert parse_10k(doc).income_statement is None


# ── real filings, when the cache has them ────────────────────────────────────

def _cached_filings():
    if not CACHE.exists():
        return []
    return sorted(CACHE.glob("*/*.html"))


@pytest.mark.parametrize("path", _cached_filings(), ids=lambda p: p.parent.name)
def test_real_filing_parses(path):
    if not _cached_filings():
        pytest.skip("no cached filings; run scripts/ingest_demo.py first")

    parsed = parse_10k(path.read_text(encoding="utf-8"))

    assert parsed.risk_factors and parsed.risk_factors.char_count > 20_000
    assert parsed.mdna and parsed.mdna.char_count > 5_000
    assert "Table of Contents" not in parsed.risk_factors.text

    table = parsed.income_statement
    assert table is not None and len(table.periods) >= 2
    labels = {r.label.lower().rstrip(":") for r in table.rows}
    assert "net income" in labels
    assert any(term in labels for term in ("revenue", "total net sales"))
