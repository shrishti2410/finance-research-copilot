"""Turn a 10-K's raw HTML into structured sections.

Sections are located by **document structure**, not by regex over flattened
text. The distinction matters because every 10-K contains each item heading at
least twice -- once in the table of contents, once at the real section -- and a
text-level search reliably returns the first, giving you a page-number list
instead of the section.

The structural discriminator, verified against NVDA and AAPL:

    table of contents   <table><tr><td><div><span><a>Item 1A.</a>...
    real section        <body><div><span>Item 1A. Risk Factors</span></div>

The real heading is a block element that is a direct child of `<body>` and has
no `<table>` ancestor. The TOC copy is always inside a table, because that is
how a TOC is laid out. Filtering on "no table ancestor" leaves exactly one
candidate per item in both filings.

Once the markers are known, a section is the run of body-level blocks between
its heading and the next item's heading -- so the boundaries come from the
document's own ordering rather than from guessing where prose stops.

Nothing here chunks or embeds; that is `chunker.py` and `rag/embeddings.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import lxml.html

# Item headings in the order Form 10-K mandates. Used to find each section's end:
# a section runs until the next item that actually appears in the document.
ITEM_ORDER = [
    "1", "1A", "1B", "1C", "2", "3", "4",
    "5", "6", "7", "7A", "8",
    "9", "9A", "9B", "9C", "10", "11", "12", "13", "14", "15", "16",
]

ITEM_TITLES = {
    "1A": "Risk Factors",
    "7": "Management's Discussion and Analysis of Financial Condition and Results of Operations",
    "8": "Financial Statements and Supplementary Data",
}

# A heading is short. Anything longer is a paragraph that merely starts by
# referring to an item ("Item 1A. Risk Factors below describes...").
MAX_HEADING_CHARS = 200

# Rows/labels that identify a consolidated statement of operations, as opposed
# to the dozens of other tables in a filing (segment data, leases, stock comp).
INCOME_STATEMENT_TERMS = [
    "revenue", "net sales", "cost of revenue", "cost of sales", "cost of goods",
    "gross profit", "gross margin", "operating expenses", "research and development",
    "operating income", "income from operations", "net income", "earnings per share",
    "provision for income taxes", "income before income taxes",
]

# Tables that look like an income statement but are not the statement itself.
DISQUALIFYING_TERMS = ["segment", "geographic", "disaggregat", "by category"]

_WS = re.compile(r"\s+")
_NUMERIC = re.compile(r"^\(?-?[\d,]+(\.\d+)?\)?%?$")
_YEAR = re.compile(r"(19|20)\d{2}")
_DASHES = {"—", "–", "-", "—", "–"}


def _norm(text: str | None) -> str:
    return _WS.sub(" ", (text or "")).strip()


@dataclass(frozen=True)
class Section:
    """One Item section of the filing."""

    item: str
    title: str
    heading: str          # the heading text as it actually appears
    text: str
    char_count: int
    block_count: int      # body-level blocks the section spans
    boilerplate_blocks_dropped: int = 0

    def preview(self, n: int = 500) -> str:
        return self.text[:n]


# What a row's numbers *are*, which decides the multiplier that applies to them.
# A statement of operations mixes three kinds under one units note, and treating
# them alike is wrong by a factor of a thousand or a million.
ROW_AMOUNT = "amount"            # money: revenue, cost, net income
ROW_PER_SHARE = "per_share"      # already dollars per share; never scaled
ROW_SHARE_COUNT = "share_count"  # a count of shares, often scaled differently
ROW_HEADING = "heading"          # a grouping label with no numbers of its own


@dataclass(frozen=True)
class TableRow:
    label: str
    values: list[float | None]
    raw_cells: list[str] = field(default_factory=list)
    kind: str = ROW_AMOUNT


_SCALE_WORDS = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_SCALE_WORD = re.compile(r"\b(thousand|million|billion)s?\b", re.I)
# "number of shares", "shares in thousands", "share data" -- the point in the
# units note where it stops talking about money and starts talking about shares.
_SHARE_MENTION = re.compile(r"\bshares?\b", re.I)


@dataclass(frozen=True)
class UnitsScale:
    """The multipliers one units note implies, per kind of row.

    Apple's note is the case that forces this apart:

        "In millions, except number of shares, which are reflected in
         thousands, and per-share amounts"

    Three different scales in one sentence -- amounts at 1e6, share counts at
    1e3, and per-share figures at 1. NVIDIA's shorter note, "In millions, except
    per share data", names no separate share scale, and its share counts really
    are in millions (24,359 -> 24.36 billion shares, which is what NVIDIA has).
    So an unnamed share scale must fall back to the amount scale, not to
    thousands.
    """

    amount: int = 1
    share_count: int = 1
    # Never scaled. "$7.49 per share" is already the figure; multiplying it by
    # the table's amount scale produces $7,490,000 per share.
    per_share: int = 1

    def for_kind(self, kind: str) -> int:
        if kind == ROW_PER_SHARE:
            return self.per_share
        if kind == ROW_SHARE_COUNT:
            return self.share_count
        return self.amount


def parse_units_scale(units: str) -> UnitsScale:
    """Read a units note into per-kind multipliers.

    The leading scale word governs money. A scale word appearing *after* the
    first mention of shares governs share counts; without one, share counts
    inherit the money scale.
    """
    if not units:
        return UnitsScale()

    first = _SCALE_WORD.search(units)
    amount = _SCALE_WORDS[first.group(1).lower()] if first else 1

    share_count = amount
    mention = _SHARE_MENTION.search(units, first.end() if first else 0)
    if mention:
        after = _SCALE_WORD.search(units, mention.end())
        if after:
            share_count = _SCALE_WORDS[after.group(1).lower()]

    return UnitsScale(amount=amount, share_count=share_count)


# Checked in this order: the share-count phrasing usually contains "per share"
# too ("Weighted average shares used in per share computation"), so the more
# specific pattern has to win.
_SHARE_COUNT_LABEL = re.compile(
    r"shares?\s+(used|outstanding)|weighted[- ]average\s+(number\s+of\s+)?shares"
    r"|number\s+of\s+shares|shares?\s+used\s+in", re.I
)
_PER_SHARE_LABEL = re.compile(r"per[- ]share|per\s+common\s+share|earnings\s+per", re.I)


def _row_kind(label: str) -> str | None:
    """What this label says about itself, if anything."""
    if _SHARE_COUNT_LABEL.search(label):
        return ROW_SHARE_COUNT
    if _PER_SHARE_LABEL.search(label):
        return ROW_PER_SHARE
    return None


def classify_rows(rows: list[TableRow]) -> list[TableRow]:
    """Tag each row with what its numbers are.

    Most rows do not say. "Basic" appears twice in every statement of
    operations -- once under "Net income per share:" and once under "Weighted
    average shares used in per share computation:" -- with identical labels and
    a factor of a billion between them. The only thing separating them is the
    grouping row above, so that heading is carried down as context until the
    next one replaces it.
    """
    classified: list[TableRow] = []
    context: str | None = None

    for row in rows:
        declared = _row_kind(row.label)
        has_numbers = any(v is not None for v in row.values)

        if not has_numbers:
            # A grouping label. It sets the context for the rows beneath it, and
            # a heading that says nothing about shares clears a stale context.
            context = declared
            kind = ROW_HEADING
        else:
            kind = declared or context or ROW_AMOUNT

        classified.append(
            TableRow(label=row.label, values=row.values, raw_cells=row.raw_cells, kind=kind)
        )

    return classified


@dataclass(frozen=True)
class FinancialTable:
    """A parsed financial statement table."""

    title: str
    units: str                    # e.g. "In millions, except per share data"
    periods: list[str]            # column headers, e.g. ["Jan 25, 2026", ...]
    rows: list[TableRow]
    row_count: int
    numeric_cell_count: int
    match_score: int              # how strongly it matched income-statement terms

    @property
    def scales(self) -> "UnitsScale":
        """The multipliers this table's units note implies, by row kind."""
        return parse_units_scale(self.units)

    @property
    def default_scale(self) -> int:
        """Multiplier for ordinary money rows.

        Named `default_scale`, not `scale`, on purpose. A statement of
        operations has no single scale: Apple states amounts in millions, share
        counts in thousands and earnings per share in dollars, all under one
        units note. A property called `scale` invites `row.values * table.scale`,
        which is how 14.9 billion shares becomes 14.9 trillion. Use
        `scale_for(row)` or `absolute_values(row)`.
        """
        return self.scales.amount

    def scale_for(self, row: TableRow) -> int:
        """The multiplier that applies to this specific row."""
        return self.scales.for_kind(row.kind)

    def absolute_values(self, row: TableRow) -> list[float | None]:
        """This row's figures in absolute units -- dollars, or shares."""
        factor = self.scale_for(row)
        return [None if v is None else v * factor for v in row.values]

    def as_records(self) -> list[dict]:
        return [{"label": r.label, **{p: v for p, v in zip(self.periods, r.values)}}
                for r in self.rows]


@dataclass(frozen=True)
class ParsedFiling:
    risk_factors: Section | None
    mdna: Section | None
    income_statement: FinancialTable | None
    items_found: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Locating item headings
# ─────────────────────────────────────────────────────────────────────────────

def _body_blocks(root) -> list:
    body = root.find("body")
    return list(body) if body is not None else list(root)


def find_item_markers(blocks: list) -> dict[str, int]:
    """Map item id -> index into `blocks` of its real heading.

    Skips any candidate with a `<table>` ancestor, which is what removes the
    table-of-contents copy. Where several candidates survive, the last wins:
    a cross-reference to an item always precedes the item itself.
    """
    markers: dict[str, int] = {}

    for item in ITEM_ORDER:
        # Require a delimiter after the number so "Item 1" does not swallow
        # "Item 1A", and "Item 9A" is not matched by the pattern for "Item 9".
        pattern = re.compile(rf"^item\s*{re.escape(item)}\s*[\.\:\)\-–—]?\s", re.I)

        for index, block in enumerate(blocks):
            text = _norm(block.text_content())
            if len(text) > MAX_HEADING_CHARS:
                continue
            if not pattern.match(text + " "):
                continue
            if block.xpath("ancestor::table"):
                continue  # table of contents
            markers[item] = index

    return markers


def _section_end(markers: dict[str, int], start_item: str) -> int | None:
    """Index of the next item heading that actually appears after `start_item`."""
    start_index = markers[start_item]
    later = [i for item, i in markers.items() if i > start_index]
    return min(later) if later else None


# Page furniture that sits between paragraphs in the flow: a bare page number,
# the "Table of Contents" link back to the top, and pipe-delimited running
# footers such as "Apple Inc. | 2025 Form 10-K | 28".
_PAGE_NUMBER = re.compile(r"^\d{1,4}$")
_RUNNING_FOOTER = re.compile(r"^.{0,60}\|\s*\d{1,4}\s*$")
_TOC_LINK = re.compile(r"^table of contents$", re.I)

# A short line repeated this many times across the filing is furniture, not
# prose. Catches per-filer running headers ("NVIDIA Corporation and
# Subsidiaries") without needing a rule for each filer.
BOILERPLATE_MIN_REPEATS = 5
BOILERPLATE_MAX_CHARS = 80


def find_boilerplate(blocks: list) -> frozenset[str]:
    """Short block texts that repeat often enough to be page furniture."""
    counts: dict[str, int] = {}
    for block in blocks:
        text = _norm(block.text_content())
        if text and len(text) <= BOILERPLATE_MAX_CHARS:
            counts[text] = counts.get(text, 0) + 1
    return frozenset(t for t, n in counts.items() if n >= BOILERPLATE_MIN_REPEATS)


def _is_boilerplate(text: str, repeated: frozenset[str]) -> bool:
    if text in repeated:
        return True
    if len(text) > BOILERPLATE_MAX_CHARS:
        return False
    return bool(
        _PAGE_NUMBER.match(text) or _TOC_LINK.match(text) or _RUNNING_FOOTER.match(text)
    )


_TERMINAL = tuple(".!?:;”\"')") + ("]",)


def _stitch_fragments(paragraphs: list[str]) -> list[str]:
    """Rejoin sentences that the filing split across two blocks.

    Filings break a sentence across a page boundary, so the source has
    "...acts of war or other military" in one block and "actions, epidemics..."
    in the next, with a page number between them. Once that page number is
    filtered out the two halves sit adjacent, and treating them as separate
    paragraphs would let a chunk boundary land in the middle of the sentence.

    Merged only when the left side lacks terminal punctuation *and* the right
    side opens lower-case -- a heading also lacks punctuation, but the text
    after it starts with a capital, so headings are left alone.
    """
    if not paragraphs:
        return paragraphs

    stitched = [paragraphs[0]]
    for para in paragraphs[1:]:
        previous = stitched[-1]
        if not previous.endswith(_TERMINAL) and para[:1].islower():
            stitched[-1] = f"{previous} {para}"
        else:
            stitched.append(para)
    return stitched


def extract_section(
    blocks: list,
    markers: dict[str, int],
    item: str,
    repeated: frozenset[str] = frozenset(),
) -> Section | None:
    if item not in markers:
        return None

    start = markers[item]
    end = _section_end(markers, item)
    end = end if end is not None else len(blocks)

    heading = _norm(blocks[start].text_content())

    # Blocks after the heading, up to the next item heading. Blank blocks are
    # dropped (filings are full of spacer divs), and so is page furniture --
    # otherwise a page break drops "Table of Contents" and a page number into
    # the middle of a paragraph, and that lands verbatim in a chunk.
    paragraphs: list[str] = []
    dropped = 0
    for block in blocks[start + 1:end]:
        text = _norm(block.text_content())
        if not text:
            continue
        if _is_boilerplate(text, repeated):
            dropped += 1
            continue
        paragraphs.append(text)

    paragraphs = _stitch_fragments(paragraphs)
    body_text = "\n\n".join(paragraphs)
    return Section(
        item=item,
        title=ITEM_TITLES.get(item, heading),
        heading=heading,
        text=body_text,
        char_count=len(body_text),
        block_count=end - start - 1,
        boilerplate_blocks_dropped=dropped,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Financial tables
# ─────────────────────────────────────────────────────────────────────────────

def _parse_number(cell: str) -> float | None:
    """'$ 130,497' -> 130497.0, '(1,234)' -> -1234.0, '—' -> None."""
    text = cell.strip().replace("$", "").replace("\xa0", " ").strip()
    if not text or text in _DASHES:
        return None
    # Accounting negatives are parenthesised, not signed.
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").replace("%", "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def _table_rows(table) -> list[TableRow]:
    """One TableRow per `<tr>`.

    Financial HTML tables interleave real data with layout cells: a separate
    cell for the '$', empty spacer cells between columns, a cell holding only
    ')'. The label is the first cell with non-numeric text; values are every
    later cell that parses as a number. Empty and symbol-only cells are simply
    skipped rather than emitted as None, or every row would carry a dozen
    phantom columns.
    """
    rows: list[TableRow] = []

    for tr in table.xpath(".//tr"):
        cells = [_norm(td.text_content()) for td in tr.xpath("./td|./th")]
        if not cells:
            continue

        label = ""
        values: list[float | None] = []
        for cell in cells:
            if not cell or cell in {"$", "%", ")", "("} or cell in _DASHES:
                continue
            number = _parse_number(cell)
            if number is None and not label:
                label = cell
            elif number is not None:
                values.append(number)

        if label or values:
            rows.append(TableRow(label=label, values=values, raw_cells=cells))

    return rows


def _score_table(rows: list[TableRow], context: str = "") -> int:
    """How much this table looks like a statement of operations.

    `context` is the text surrounding the table, and it is checked for
    disqualifiers alongside the row labels: a segment-information note carries
    income-statement line items but only says 'Segment Information' in a heading
    above the table, never in its rows.
    """
    labels = " ".join(r.label.lower() for r in rows)
    haystack = f"{labels} {context.lower()}"
    if any(term in haystack for term in DISQUALIFYING_TERMS):
        return 0
    return sum(1 for term in INCOME_STATEMENT_TERMS if term in labels)


def _table_periods(table, rows: list[TableRow]) -> list[str]:
    """Column headers, taken from the first row that is mostly years.

    10-K statement headers are usually a 'Year Ended ...' caption row followed
    by the fiscal years; the years are the useful part.
    """
    for tr in table.xpath(".//tr")[:8]:
        cells = [_norm(td.text_content()) for td in tr.xpath("./td|./th")]
        # Period columns are rarely a bare year. NVDA writes "Jan 25, 2026" and
        # AAPL "September 27,2025", so match any short cell containing a year
        # rather than requiring the cell to *be* one.
        dated = [c for c in cells if c and len(c) <= 30 and _YEAR.search(c)]
        if len(dated) >= 2:
            return dated

    width = max((len(r.values) for r in rows), default=0)
    return [f"col{i + 1}" for i in range(width)]


def _strip_header_rows(rows: list[TableRow], periods: list[str]) -> list[TableRow]:
    """Drop the leading caption rows ('Year Ended', 'Jan 25, 2026').

    They carry no values and only repeat what `periods` already says. Only
    leading rows are considered, so genuine label-only grouping rows further
    down ('Operating expenses:') survive.
    """
    period_set = {p.lower() for p in periods}
    start = 0
    for row in rows:
        if row.values:
            break
        label = row.label.lower()
        if label in period_set or "ended" in label or _YEAR.search(label):
            start += 1
            continue
        break
    return rows[start:]


_STATEMENT_CAPTION = re.compile(
    r"consolidated statements?\s+of\s+(income|operations|earnings)", re.I
)
# "(In millions, except per share data)" -- the scale every figure is stated in.
# Without it the numbers are ambiguous by a factor of a thousand.
_UNITS = re.compile(r"\(?\s*in\s+(thousands|millions|billions)[^)]*\)?", re.I)


def _units_for(blocks: list, index: int, floor: int, lookback: int = 8) -> str:
    """The units note sitting just above a financial table, if there is one."""
    for j in range(index - 1, max(floor - 1, index - lookback - 1), -1):
        text = _norm(blocks[j].text_content())
        if text and len(text) <= 120:
            found = _UNITS.search(text)
            if found:
                return found.group(0).strip("() ")
    return ""


def _caption_and_context(
    blocks: list, index: int, floor: int, lookback: int = 8
) -> tuple[str, str]:
    """The heading above a table, plus the surrounding text used to disqualify it.

    Returns (caption, context). The caption is the single best title -- a real
    "Consolidated Statements of Income" heading if one is in range, otherwise
    the nearest short line, which is usually the units note.

    The context is *every* short line in the lookback window joined together,
    and it is what disqualifiers are matched against. Checking only the caption
    is not enough: a segment note is titled "Note 13 - Segment Information" but
    the line directly above its table is the units note, so the giveaway word
    sits two blocks up and a caption-only check misses it entirely.
    """
    caption = ""
    nearby: list[str] = []
    for j in range(index - 1, max(floor - 1, index - lookback - 1), -1):
        text = _norm(blocks[j].text_content())
        if not text or len(text) > 120:
            continue
        nearby.append(text)
        if _STATEMENT_CAPTION.search(text) and not _STATEMENT_CAPTION.search(caption):
            caption = text
        elif not caption:
            caption = text
    return caption, " ".join(nearby)


def extract_income_statement(
    blocks: list, markers: dict[str, int]
) -> FinancialTable | None:
    """Best income-statement candidate at or after Item 8.

    The search starts at the Item 8 heading and runs to the *end of the
    document*, not to Item 9. Both layouts in the wild need that:

      * AAPL puts the statements inside Item 8, after an index table.
      * NVDA's Item 8 is a one-line cross-reference ("The information required
        by this Item is set forth in our Consolidated Financial Statements"),
        with the statements themselves in the F-pages after Item 9.

    Starting at Item 8 still excludes MD&A, whose revenue summary tables would
    otherwise outrank the audited statement.
    """
    start = markers.get("8", 0)
    best: FinancialTable | None = None

    for index in range(start, len(blocks)):
        block = blocks[index]
        tables = block.xpath(".//table")
        if block.tag == "table":
            tables = [block, *tables]

        for table in tables:
            rows = _table_rows(table)
            if len(rows) < 4:
                continue

            caption, context = _caption_and_context(blocks, index, start)
            score = _score_table(rows, context)
            if score < 4:  # several statement lines, not one stray mention
                continue

            numeric_cells = sum(len(r.values) for r in rows)
            if numeric_cells < 6:
                continue

            periods = _table_periods(table, rows)
            data_rows = classify_rows(_strip_header_rows(rows, periods))

            candidate = FinancialTable(
                title=caption if _STATEMENT_CAPTION.search(caption)
                else "Income statement (detected by row labels)",
                units=_units_for(blocks, index, start),
                periods=periods,
                rows=data_rows,
                row_count=len(data_rows),
                numeric_cell_count=numeric_cells,
                match_score=score,
            )
            # Highest score wins; ties go to the earliest, because the income
            # statement is the first statement presented in every 10-K.
            if best is None or candidate.match_score > best.match_score:
                best = candidate

    return best


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)


def _to_document(html: str | bytes):
    """Parse filing markup into an element tree.

    Inline-XBRL filings (every recent 10-K) open with `<?xml version='1.0'
    encoding='ASCII'?>`, and lxml refuses a `str` carrying an encoding
    declaration. Bytes are handed over as-is so lxml can honour the declaration;
    a `str` has the declaration stripped, which is safe because these documents
    hold every non-ASCII character as an entity (`&#8217;`) rather than a raw
    byte.

    The HTML parser is used deliberately, not the XML one: the documents carry
    an XHTML namespace, and parsing them as XML would namespace every tag and
    quietly break plain `//table` and `//tr` lookups.
    """
    if isinstance(html, bytes):
        return lxml.html.fromstring(html)
    return lxml.html.fromstring(_XML_DECL.sub("", html, count=1))


def parse_10k(html: str | bytes) -> ParsedFiling:
    """Parse a 10-K's primary HTML document into structured sections."""
    root = _to_document(html)
    blocks = _body_blocks(root)
    markers = find_item_markers(blocks)
    repeated = find_boilerplate(blocks)

    return ParsedFiling(
        risk_factors=extract_section(blocks, markers, "1A", repeated),
        mdna=extract_section(blocks, markers, "7", repeated),
        income_statement=extract_income_statement(blocks, markers),
        items_found=sorted(markers, key=ITEM_ORDER.index),
    )
