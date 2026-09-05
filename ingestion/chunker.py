"""Split parsed filings into retrieval units.

Two kinds of chunk come out of here, and they are built by different rules
because they are different kinds of thing:

  * **Prose sections** (Item 1A, Item 7) are packed into ~500-token chunks with
    ~50 tokens of overlap.
  * **Financial tables** are emitted as exactly one chunk each, never split.
    Cutting a statement of operations in half severs line items from their
    column headers, and half an income statement is worse than none: it still
    retrieves, and it still looks plausible.

**Token counts are real**, from the Qwen2.5-1.5B-Instruct tokenizer -- the model
this project actually serves. Word counts are not a proxy for this: filings are
dense with numbers, tickers and legal boilerplate that tokenize far worse than
prose, so a "500-word" chunk can be 900 tokens and silently blow a context
budget.

**Chunks end on sentence boundaries.** Units are paragraphs, split into
sentences only when a paragraph alone exceeds the target. A chunk is a whole
number of those units, so a chunk boundary is never mid-sentence unless one
sentence is itself longer than the target -- which is handled explicitly and
flagged on the chunk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing transformers just to type-check
    from ingestion.edgar_client import Filing
    from ingestion.parser import FinancialTable, ParsedFiling, Section

TOKENIZER_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"

TARGET_TOKENS = 500
OVERLAP_TOKENS = 50

# Sentence boundary: terminal punctuation, then whitespace, then something that
# starts a new sentence. The lookahead keeps decimals ("$1.5 billion") and
# mid-sentence initials from splitting.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+(?=[\"“'(A-Z])")

# Tokens that end in a period without ending a sentence. Filings are full of
# these -- "U.S.", "Inc.", "No." -- and splitting on them produces fragments
# that read as truncated mid-thought.
_ABBREVIATIONS = {
    "u.s", "u.k", "e.u", "inc", "corp", "co", "ltd", "llc", "plc", "no", "nos",
    "mr", "mrs", "ms", "dr", "jr", "sr", "st", "vs", "etc", "e.g", "i.e",
    "approx", "fig", "al", "dept", "est", "ch", "sec", "art",
}


@lru_cache(maxsize=2)
def get_tokenizer(model_id: str = TOKENIZER_MODEL):
    """The real tokenizer, loaded once per process.

    Imported lazily so that `import ingestion.chunker` does not drag in
    transformers -- the EDGAR client and parser have no use for it, and it is a
    heavy import for a module that only fetches HTML.
    """
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(model_id)


def count_tokens(text: str, model_id: str = TOKENIZER_MODEL) -> int:
    return len(get_tokenizer(model_id).encode(text, add_special_tokens=False))


# ─────────────────────────────────────────────────────────────────────────────
# Chunk model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChunkMetadata:
    """Travels with every chunk into the vector store.

    The five required fields come first; the rest are provenance, so a retrieved
    passage can always be traced back to a specific document on sec.gov.
    """

    company: str
    filing_type: str
    fiscal_period: str
    section: str
    chunk_index: int

    ticker: str = ""
    cik: str = ""
    accession: str = ""
    period_end: str = ""
    filing_date: str = ""
    source_url: str = ""
    content_type: str = "text"     # "text" | "table"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    content: str
    metadata: ChunkMetadata
    token_count: int
    # Table chunks keep their parsed form alongside the rendered text, so a tool
    # can do arithmetic on the numbers instead of re-parsing the string.
    structured: dict | None = None
    # True when a single sentence exceeded the target and had to be cut by token
    # count. Surfaced rather than hidden -- it is the one case where a chunk can
    # end mid-sentence.
    hard_split: bool = False

    def __repr__(self) -> str:
        return (f"<Chunk {self.chunk_id} {self.metadata.section} "
                f"#{self.metadata.chunk_index} {self.token_count}tok>")


# ─────────────────────────────────────────────────────────────────────────────
# Splitting prose into units
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Unit:
    text: str
    tokens: int
    paragraph: int          # so units are rejoined with the right separator
    hard_split: bool = False


def split_sentences(paragraph: str) -> list[str]:
    """Sentence split that does not fire on common filing abbreviations."""
    pieces = _SENTENCE_BREAK.split(paragraph)

    merged: list[str] = []
    for piece in pieces:
        if merged:
            tail = merged[-1].rstrip()
            last_word = tail.split()[-1].lower().rstrip(".") if tail.split() else ""
            # "...the U.S. Securities and Exchange Commission" must stay whole,
            # and so must a fragment that begins lowercase.
            if last_word in _ABBREVIATIONS or (piece[:1].islower()):
                merged[-1] = f"{merged[-1]} {piece}"
                continue
        merged.append(piece)
    return [m.strip() for m in merged if m.strip()]


HEADING_MAX_TOKENS = 15
_TERMINAL_PUNCT = (".", "!", "?", ":", ";", "”", '"', "'", ")")


def _is_heading(unit: _Unit) -> bool:
    """Whether a unit reads as a section heading rather than a sentence.

    Headings are short, carry no terminal punctuation, and are not bullets.
    Used only to avoid stranding one at the end of a chunk.
    """
    text = unit.text.strip()
    if not text or unit.tokens > HEADING_MAX_TOKENS:
        return False
    if text.startswith(("•", "-", "–")):
        return False
    return not text.endswith(_TERMINAL_PUNCT)


def _hard_split(text: str, model_id: str, limit: int) -> list[str]:
    """Last resort for a single sentence longer than the target.

    Splits on decoded token boundaries, so no character is lost and no token is
    mangled -- but it *will* land mid-sentence, which is why callers mark the
    resulting chunks `hard_split=True`.
    """
    tokenizer = get_tokenizer(model_id)
    ids = tokenizer.encode(text, add_special_tokens=False)
    return [
        tokenizer.decode(ids[i:i + limit], skip_special_tokens=True)
        for i in range(0, len(ids), limit)
    ]


def _build_units(text: str, model_id: str, target: int) -> list[_Unit]:
    """Paragraphs, broken down only as far as necessary.

    A paragraph that already fits stays whole -- that is what keeps chunks
    readable. Only oversized paragraphs get sentence-split, and only oversized
    sentences get token-split.
    """
    units: list[_Unit] = []

    for para_index, paragraph in enumerate(text.split("\n\n")):
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        tokens = count_tokens(paragraph, model_id)
        if tokens <= target:
            units.append(_Unit(paragraph, tokens, para_index))
            continue

        for sentence in split_sentences(paragraph):
            sentence_tokens = count_tokens(sentence, model_id)
            if sentence_tokens <= target:
                units.append(_Unit(sentence, sentence_tokens, para_index))
                continue
            for fragment in _hard_split(sentence, model_id, target):
                units.append(
                    _Unit(fragment, count_tokens(fragment, model_id), para_index, hard_split=True)
                )

    return units


def _join(units: list[_Unit]) -> str:
    """Rejoin units, restoring the paragraph breaks they came from."""
    out = ""
    for i, unit in enumerate(units):
        if i == 0:
            out = unit.text
        elif unit.paragraph != units[i - 1].paragraph:
            out += "\n\n" + unit.text
        else:
            out += " " + unit.text
    return out


def _overlap_units(units: list[_Unit], overlap: int, model_id: str) -> list[_Unit]:
    """Trailing units of a chunk totalling at most `overlap` tokens.

    Overlap is whole units, so the repeated span is complete sentences rather
    than an arbitrary token window starting mid-clause.

    Whole units alone are not enough in practice: a chunk usually ends on a
    full paragraph, and a paragraph is normally larger than the overlap budget,
    so nothing would carry over and most boundaries would get no overlap at all.
    When that happens the final unit is re-split into sentences and its trailing
    sentences are carried instead.
    """
    carried: list[_Unit] = []
    total = 0
    for unit in reversed(units):
        if total + unit.tokens > overlap:
            break
        carried.insert(0, unit)
        total += unit.tokens

    if carried:
        return carried

    tail: list[str] = []
    total = 0
    last = units[-1]
    for sentence in reversed(split_sentences(last.text)):
        tokens = count_tokens(sentence, model_id)
        if total + tokens > overlap:
            break
        tail.insert(0, sentence)
        total += tokens

    # Still nothing means even the closing sentence is longer than the overlap
    # budget. Carrying a partial sentence would defeat the point, so this
    # boundary simply gets no overlap.
    if not tail:
        return []
    return [_Unit(" ".join(tail), total, last.paragraph)]


# ─────────────────────────────────────────────────────────────────────────────
# Chunking
# ─────────────────────────────────────────────────────────────────────────────

def chunk_section(
    section: "Section",
    base_metadata: dict,
    start_index: int = 0,
    target_tokens: int = TARGET_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
    model_id: str = TOKENIZER_MODEL,
) -> list[Chunk]:
    """Pack one prose section into overlapping ~target_tokens chunks."""
    units = _build_units(section.text, model_id, target_tokens)
    if not units:
        return []

    chunks: list[Chunk] = []
    current: list[_Unit] = []
    running = 0
    index = start_index

    def flush() -> None:
        nonlocal current, running, index
        if not current:
            return
        content = _join(current)
        chunks.append(
            Chunk(
                chunk_id=f"{base_metadata['accession']}:{section.item}:{index}",
                content=content,
                # Re-encoded rather than summed: BPE merges across unit
                # boundaries, so the sum of the parts is an estimate and this
                # is the number that actually matters downstream.
                token_count=count_tokens(content, model_id),
                metadata=ChunkMetadata(
                    section=f"Item {section.item} - {section.title}",
                    chunk_index=index,
                    content_type="text",
                    **base_metadata,
                ),
                hard_split=any(u.hard_split for u in current),
            )
        )
        index += 1

    for unit in units:
        if current and running + unit.tokens > target_tokens:
            # A heading that landed last would otherwise be stranded at the end
            # of this chunk, separated from the text it introduces. Hold it back
            # and open the next chunk with it instead.
            held = current.pop() if len(current) > 1 and _is_heading(current[-1]) else None

            flush()
            current = _overlap_units(current, overlap_tokens, model_id)
            if held is not None:
                current.append(held)
            running = sum(u.tokens for u in current)

        current.append(unit)
        running += unit.tokens

    flush()
    return chunks


# Query vocabulary that does not appear in a filing's own line-item labels.
# A statement says "Revenue" and "Net income"; people ask about "sales", "the
# top line", "EPS", "profitability". The caption bridges the two, which is the
# whole reason a table needs one -- a grid of digits has almost no surface for a
# natural-language query to match on.
#
# Each entry is (labels that trigger it, the phrase to emit). Triggers are a
# *list* because companies name the same line differently and the caption has to
# read the same either way: Apple's statement says "Total net sales" and "Gross
# margin" where NVIDIA's says "Revenue" and "Gross profit". Keying only on
# NVIDIA's wording left Apple's caption with no mention of revenue at all --
# which is precisely the query this exists to serve.
_CAPTION_SYNONYMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("revenue", "net sales", "total net sales", "sales"),
     "total revenue, net sales, turnover, the top line"),
    (("cost of revenue", "cost of sales", "cost of goods"),
     "cost of revenue, cost of sales, cost of goods sold, COGS"),
    (("gross profit", "gross margin"),
     "gross profit, gross margin, gross profitability"),
    (("operating income", "income from operations", "operating expenses"),
     "operating income, operating profit, income from operations, operating margin"),
    (("net income", "net earnings", "net profit"),
     "net income, net profit, earnings, the bottom line, profit after tax"),
    (("per share",),
     "earnings per share, EPS, basic and diluted EPS"),
    (("shares",),
     "share count, weighted average shares outstanding"),
    (("income tax", "provision for income"),
     "income tax expense, provision for income taxes, effective tax rate"),
    (("research and development",),
     "research and development, R&D spending"),
)

# Line items worth naming with their actual figure. A caption carrying
# "total revenue of 215,938 million" gives a numeric query something to match
# that the bare grid does not.
_HEADLINE_LABELS = ("revenue", "net sales", "total net sales", "gross profit",
                    "gross margin", "operating income", "net income")


def table_caption(table: "FinancialTable", base_metadata: dict) -> str:
    """A natural-language description of what a table contains.

    Prepended to the rendered grid before embedding. Without it a table chunk is
    almost pure digits, and a conversational question -- "what was total
    revenue?" -- embeds close to prose *about* revenue and far from the
    statement that actually holds the number. This is the sentence a person
    would write above the table if they were introducing it.

    Built from the table's own row labels, not a fixed template, so a statement
    with different line items describes itself accordingly.
    """
    company = base_metadata.get("company", "")
    ticker = base_metadata.get("ticker", "")
    period = base_metadata.get("fiscal_period", "")
    who = f"{company} ({ticker})" if company and ticker else (company or ticker)

    title = table.title if "detected by row labels" not in table.title else         "Consolidated income statement"

    parts = [
        f"{title} for {who}, {period}."
        if who else f"{title}, {period}."
    ]
    if table.periods:
        parts.append(
            f"Annual financial results for the periods ending "
            f"{', '.join(table.periods)}."
        )

    labels = [row.label for row in table.rows if row.label]
    lowered = " ".join(labels).lower()

    # Only claim the concepts this table actually reports.
    present = [
        phrase for triggers, phrase in _CAPTION_SYNONYMS
        if any(trigger in lowered for trigger in triggers)
    ]
    if present:
        parts.append("Line items include " + "; ".join(present) + ".")

    headline = []
    for row in table.rows:
        if row.label.lower().strip(":") in _HEADLINE_LABELS and row.values:
            value = row.values[0]
            if value is not None:
                headline.append(f"{row.label} {value:,.0f}")
    if headline:
        unit = table.units or "as reported"
        parts.append(f"Most recent period: {'; '.join(headline[:5])} ({unit}).")

    return " ".join(parts)


def chunk_table(
    table: "FinancialTable",
    base_metadata: dict,
    index: int,
    model_id: str = TOKENIZER_MODEL,
) -> Chunk:
    """One chunk per table, never split.

    The content is a rendered text view so the chunk can be embedded like any
    other, and `structured` carries the parsed rows so a tool can compute on the
    numbers without re-parsing the rendering.
    """
    # The caption goes into `content`, not into a separate embedding-only field.
    # It is genuinely useful to whoever reads the chunk -- a bare grid does not
    # say whose statement it is or what period it covers -- so there is no
    # reason to hide it from the model and show it only to the encoder.
    caption = table_caption(table, base_metadata)
    content = f"{caption}\n\n{render_table(table)}"
    return Chunk(
        chunk_id=f"{base_metadata['accession']}:income_statement:{index}",
        content=content,
        token_count=count_tokens(content, model_id),
        metadata=ChunkMetadata(
            section="Income Statement",
            chunk_index=index,
            content_type="table",
            **base_metadata,
        ),
        structured={
            "title": table.title,
            "units": table.units,
            # Per row, not per table. One statement mixes money, share counts
            # and per-share figures under a single units note, so a table-wide
            # "scale" key is an invitation to multiply an EPS by a million.
            "scales": {
                "amount": table.scales.amount,
                "share_count": table.scales.share_count,
                "per_share": table.scales.per_share,
            },
            "periods": table.periods,
            "rows": [
                {
                    "label": row.label,
                    "values": row.values,          # as printed in the filing
                    "kind": row.kind,
                    "scale": table.scale_for(row),
                    # Absolute dollars or absolute share counts, so a tool can
                    # compute without having to know the convention.
                    "values_absolute": table.absolute_values(row),
                }
                for row in table.rows
            ],
        },
    )


def render_table(table: "FinancialTable") -> str:
    """Render a financial table as aligned text.

    Every row keeps its label on the same line as its figures, and the period
    headers sit directly above the columns, so the label-to-number association
    survives being flattened into a string for embedding. Rendering as prose
    ("Revenue was 215,938 in ...") would read better and retrieve worse: the
    column alignment is the part a model needs to answer "what was revenue in
    FY2025".
    """
    # Width is derived from the longest label and never truncates it. Clipping a
    # row label to fit a column would silently corrupt the text that gets
    # embedded -- "Weighted average shares used in per share computatio".
    label_width = min(max((len(r.label) for r in table.rows), default=20), 70)
    label_width = max(label_width, 20)
    # Wide enough for the longest period header, or they run together:
    # "September 27,2025September 28,2024".
    col_width = max(16, max((len(p) for p in table.periods), default=0) + 2)

    lines = [table.title]
    if table.units:
        lines.append(f"({table.units})")
    lines.append("")
    lines.append("Period".ljust(label_width) + "".join(p.rjust(col_width) for p in table.periods))
    lines.append("-" * (label_width + col_width * len(table.periods)))

    for row in table.rows:
        label = row.label.ljust(label_width)
        if not row.values:
            lines.append(label.rstrip())  # a grouping header like "Net sales:"
            continue
        cells = "".join(
            (f"{v:,.2f}" if abs(v) < 1000 and v != int(v) else f"{v:,.0f}").rjust(col_width)
            for v in row.values
        )
        lines.append(label + cells)

    return "\n".join(lines)


def _fiscal_period(report_date: date | None, filing_date: date) -> str:
    """Label a filing by fiscal year.

    Uses the period-end year, which is what companies call the fiscal year in
    both conventions seen here: NVDA's FY2026 ends Jan 2026, AAPL's FY2025 ends
    Sep 2025.
    """
    anchor = report_date or filing_date
    return f"FY{anchor.year}"


def chunk_filing(
    parsed: "ParsedFiling",
    filing: "Filing",
    target_tokens: int = TARGET_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
    model_id: str = TOKENIZER_MODEL,
) -> list[Chunk]:
    """Chunk a parsed 10-K: prose sections packed, tables kept whole."""
    base_metadata = {
        "company": filing.company_name,
        "filing_type": filing.form,
        "fiscal_period": _fiscal_period(filing.report_date, filing.filing_date),
        "ticker": filing.ticker,
        "cik": filing.cik,
        "accession": filing.accession,
        "period_end": filing.report_date.isoformat() if filing.report_date else "",
        "filing_date": filing.filing_date.isoformat(),
        "source_url": filing.document_url,
    }

    chunks: list[Chunk] = []
    # chunk_index is continuous across the document, so ordering is recoverable
    # from metadata alone after the chunks are scattered across a vector store.
    for section in (parsed.risk_factors, parsed.mdna):
        if section is None:
            continue
        chunks.extend(
            chunk_section(
                section, base_metadata, len(chunks), target_tokens, overlap_tokens, model_id
            )
        )

    if parsed.income_statement is not None:
        chunks.append(chunk_table(parsed.income_statement, base_metadata, len(chunks), model_id))

    return chunks
