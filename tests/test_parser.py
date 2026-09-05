"""Tests for the 10-K parser.

Most tests run against a small synthetic filing that reproduces the structure
that matters -- a table-of-contents table plus body-level heading divs -- so
they need no network and no 2 MB fixture. The tests at the bottom run against
real cached filings when `data/edgar/` has them, and skip otherwise.
"""

from pathlib import Path

import pytest

from ingestion.parser import (
    ROW_AMOUNT,
    ROW_HEADING,
    ROW_PER_SHARE,
    ROW_SHARE_COUNT,
    TableRow,
    classify_rows,
    parse_units_scale,
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
    # `default_scale` is the multiplier for ordinary money rows only. It used to
    # be `scale`, a single value applied to every row, which is how a $4.93 EPS
    # became $4,930,000. Per-row scale is asserted below.
    assert table.default_scale == 1_000_000


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


# ── units notes and per-row scale ────────────────────────────────────────────

# Apple's real FY2025 note. Three different scales in one sentence.
AAPL_UNITS = ("In millions, except number of shares, which are reflected in "
              "thousands, and per-share amounts")
NVDA_UNITS = "In millions, except per share data"


def test_apple_units_note_yields_three_different_scales():
    scales = parse_units_scale(AAPL_UNITS)
    assert scales.amount == 1_000_000
    assert scales.share_count == 1_000
    assert scales.per_share == 1


def test_an_unnamed_share_scale_falls_back_to_the_amount_scale():
    """NVIDIA names no separate share scale, and its share counts really are in
    millions -- 24,359 is 24.36 billion shares, which is what NVIDIA has.
    Defaulting shares to thousands would be wrong by a thousand here."""
    scales = parse_units_scale(NVDA_UNITS)
    assert scales.amount == 1_000_000
    assert scales.share_count == 1_000_000
    assert scales.per_share == 1


@pytest.mark.parametrize(
    "units,amount",
    [
        ("In thousands, except per share amounts", 1_000),
        ("In billions", 1_000_000_000),
        ("(In millions)", 1_000_000),
        ("", 1),
        ("no scale stated here", 1),
    ],
)
def test_amount_scale_from_assorted_units_notes(units, amount):
    assert parse_units_scale(units).amount == amount


def test_per_share_is_never_scaled():
    """$7.49 per share is already the figure. Scaling it by the table's amount
    scale produces $7,490,000 per share."""
    for units in (AAPL_UNITS, NVDA_UNITS, "In thousands", ""):
        assert parse_units_scale(units).per_share == 1


# ── row classification ───────────────────────────────────────────────────────

def rows(*pairs) -> list[TableRow]:
    return [TableRow(label=label, values=values) for label, values in pairs]


def test_identical_labels_are_told_apart_by_the_heading_above_them():
    """'Basic' appears twice in every statement of operations, once as an EPS
    and once as a share count, with a factor of a billion between them. The
    grouping row above is the only thing that distinguishes them."""
    classified = classify_rows(rows(
        ("Net income", [112_010.0]),
        ("Earnings per share:", []),
        ("Basic", [7.49]),
        ("Diluted", [7.46]),
        ("Shares used in computing earnings per share:", []),
        ("Basic", [14_948_500.0]),
        ("Diluted", [15_004_697.0]),
    ))
    assert [r.kind for r in classified] == [
        ROW_AMOUNT, ROW_HEADING, ROW_PER_SHARE, ROW_PER_SHARE,
        ROW_HEADING, ROW_SHARE_COUNT, ROW_SHARE_COUNT,
    ]


def test_share_count_wins_over_per_share_in_a_label_containing_both():
    """NVIDIA's heading is 'Weighted average shares used in per share
    computation:' -- it matches both patterns, and the share reading is right."""
    classified = classify_rows(rows(
        ("Weighted average shares used in per share computation:", []),
        ("Basic", [24_359.0]),
    ))
    assert classified[0].kind == ROW_HEADING
    assert classified[1].kind == ROW_SHARE_COUNT


def test_a_plain_money_heading_clears_a_stale_share_context():
    classified = classify_rows(rows(
        ("Net income per share:", []),
        ("Basic", [4.93]),
        ("Operating expenses", []),
        ("Research and development", [18_497.0]),
    ))
    assert classified[1].kind == ROW_PER_SHARE
    assert classified[3].kind == ROW_AMOUNT


def test_rows_without_any_context_are_amounts():
    assert classify_rows(rows(("Revenue", [215_938.0])))[0].kind == ROW_AMOUNT


# ── the AAPL case, end to end ────────────────────────────────────────────────

AAPL_TABLE_HTML = SYNTHETIC.replace(
    "(In millions, except per share data)", f"({AAPL_UNITS})"
).replace(
    "<tr><td>Net income</td><td></td><td>120,067</td><td>72,880</td><td>29,760</td></tr>",
    "<tr><td>Net income</td><td></td><td>112,010</td><td>112,010</td><td>112,010</td></tr>"
    "<tr><td>Earnings per share:</td></tr>"
    "<tr><td>Basic</td><td></td><td>7.49</td><td>6.11</td><td>6.13</td></tr>"
    "<tr><td>Shares used in computing earnings per share:</td></tr>"
    "<tr><td>Basic</td><td></td><td>14,948,500</td><td>15,343,783</td><td>15,744,231</td></tr>",
)


@pytest.fixture(scope="module")
def aapl_style():
    return parse_10k(AAPL_TABLE_HTML).income_statement


def test_aapl_eps_is_not_scaled(aapl_style):
    """The bug this fixes: 7.49 x 1e6 = 7,490,000 dollars per share."""
    eps = next(r for r in aapl_style.rows if r.kind == ROW_PER_SHARE)
    assert eps.values[0] == 7.49
    assert aapl_style.scale_for(eps) == 1
    assert aapl_style.absolute_values(eps)[0] == 7.49


def test_aapl_share_count_is_billions_not_trillions(aapl_style):
    """14,948,500 thousand is 14.9 billion shares. Under the old single scale it
    came out as 14.9 trillion, which is a thousand times every share in the
    S&P 500."""
    shares = next(r for r in aapl_style.rows if r.kind == ROW_SHARE_COUNT)
    assert shares.values[0] == 14_948_500.0
    assert aapl_style.scale_for(shares) == 1_000

    absolute = aapl_style.absolute_values(shares)[0]
    assert absolute == 14_948_500_000
    assert 14e9 < absolute < 15e9, f"{absolute:,.0f} is not ~14.9 billion"


def test_aapl_money_rows_still_scale_by_millions(aapl_style):
    revenue = next(r for r in aapl_style.rows if r.label == "Revenue")
    assert aapl_style.scale_for(revenue) == 1_000_000
    assert aapl_style.absolute_values(revenue)[0] == 215_938 * 1_000_000


def test_eps_times_shares_reconciles_to_net_income(aapl_style):
    """The check that ties all three scales together: if any one of them is
    wrong, this is off by orders of magnitude."""
    eps = aapl_style.absolute_values(
        next(r for r in aapl_style.rows if r.kind == ROW_PER_SHARE))[0]
    shares = aapl_style.absolute_values(
        next(r for r in aapl_style.rows if r.kind == ROW_SHARE_COUNT))[0]
    net_income = aapl_style.absolute_values(
        next(r for r in aapl_style.rows if r.label == "Net income"))[0]

    assert eps * shares == pytest.approx(net_income, rel=0.01)
