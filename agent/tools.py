"""The tools this agent may call, and how their results are shown to the model.

Five tools: `get_financial_fact` over the XBRL facts table, `search_filings`
from `rag/` (Milestone 5) and the three in `tools/` (Milestone 6). Schemas are
derived from their docstrings by `tools.registry`.

`get_financial_fact` is registered first and the descriptions point numeric
questions at it: a reported figure for a fiscal year is an exact lookup there,
where `search_filings` returns a grid the model then has to read correctly.

Full results are not what the model sees
----------------------------------------
A live `search_news` call returns 25 articles; `get_stock_price` returns up to
250 daily bars. Feeding those back verbatim would fill the context window
several times over inside two iterations, and the model would lose the system
prompt and the question before it ever reached an answer.

So each result is compacted before it becomes a tool message: the numbers and
identifiers a model needs to answer, none of the bulk. The **full** result is
kept in the trace and stored on the assistant message, so nothing is lost for
debugging or for citation -- it just does not go back through the model.

That is not a workaround for a small context. It is the same reason a person
reading a 10-K writes down the gross margin rather than the page.

A table is never cut
--------------------
Prose excerpts are cut at `EXCERPT_CHARS`; a statement is not. The indexed
income statements are ~3,100-3,300 characters, and the first ~700 of each are
the generated caption, so a 700-character excerpt of a table showed the model
the names of the line items and not one figure. Eval run-b (2026-11-10) lost
five cases to exactly that: the retriever returned the right chunk at rank 1
and the model, reading accurately, said the excerpt "lists the line items but
does not provide the specific number". A table chunk is therefore returned
whole -- caption, units note, every row -- and its parsed rows ride along in
the full result (`rows`, with `values_absolute`), so the grounding guard can
see a share count in absolute units even though the grid prints it in
millions. The model is shown the rendered grid only: it already carries the
same numbers under the units note, and on a CPU host tokens are prefill time.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from rag.retriever import search_filings as _search_filings
from tools.base import ok
from tools.facts import (
    UNKNOWN_ARGUMENT_HINT,
    fiscal_year_end,
    get_financial_fact,
    ratio_for_fiscal_year,
)
from tools.news_search import search_news
from tools.ratios import Period, RatioName
from tools.ratios import calculate_ratio as _latest_ratio
from tools.registry import Registry
from tools.stock_price import get_stock_price

# How much of a retrieved *prose* passage the model sees. Enough to carry the
# claim and its context; short enough that three of them are not the whole turn.
# Tables are exempt: see the module docstring.
EXCERPT_CHARS = 700
MAX_HEADLINES = 8
MAX_PRICE_BARS = 5

# A prose cut lands on a sentence end when one falls in the back half of the
# window, so an excerpt does not stop mid-word or mid-figure ("revenue was
# $215."). The marker says the passage continues.
_CUT_MARKER = " …"
_SENTENCE_ENDS = (". ", ".\n", "? ", "! ", "; ")


def _excerpt(content: str, content_type: str, limit: int = EXCERPT_CHARS) -> str:
    """The text of a hit as the model will see it: whole for a table, cut for prose."""
    if content_type == "table" or len(content) <= limit:
        return content
    window = content[:limit]
    cut = max((window.rfind(end) for end in _SENTENCE_ENDS), default=-1)
    if cut >= limit // 2:
        return window[:cut + 1].rstrip() + _CUT_MARKER
    return window.rstrip() + _CUT_MARKER


def _rows(structured: dict | None) -> list[dict[str, Any]]:
    """A statement chunk's parsed rows, with the fields a reader of the trace needs.

    `values` are as printed (the grid's units); `values_absolute` are in dollars
    or shares. Heading rows carry no values and are left out.
    """
    if not structured:
        return []
    return [
        {
            "label": row.get("label"),
            "kind": row.get("kind"),
            "values": row.get("values"),
            "values_absolute": row.get("values_absolute"),
        }
        for row in structured.get("rows", [])
        if row.get("kind") != "heading" and row.get("values")
    ]


async def search_filings(
    query: str,
    company: str | None = None,
    section: Literal["risk", "mdna", "income"] | None = None,
    k: int = 3,
) -> dict[str, Any]:
    """Search the text of companies' SEC 10-K annual report filings.

    Use this for what a company *said* in its annual report: how it describes
    its risks, its own explanation of results, or the exact wording of a
    disclosure, and for segment and product figures such as Apple's Services
    net sales or NVIDIA's Data Center revenue. For a company-wide reported
    figure for a fiscal year (revenue, net income, EPS, share counts, tax
    expense), call get_financial_fact first; the income statement
    (section="income") is here too, returned whole with all three fiscal
    years. For a margin or ratio, use calculate_ratio.

    Only filings that have been indexed are searchable. Currently that is NVIDIA
    (NVDA, fiscal 2026) and Apple (AAPL, fiscal 2025), one 10-K each. A search
    for any other company returns no results; that means it is not indexed, not
    that the company said nothing.

    Args:
        query: What to look for, in natural language. Describe the topic
            ("competition from other chipmakers", "supply chain concentration"),
            not just a keyword.
        company: Restrict to one company. A ticker ("NVDA") or part of the name
            ("NVIDIA"). Omit to search all indexed filings.
        section: Restrict to one part of the filing. "risk" is Item 1A Risk
            Factors, "mdna" is Item 7 Management's Discussion and Analysis, and
            "income" is the consolidated income statement. Omit to search all.
        k: How many passages to return. Defaults to 3.

    Returns:
        A dict with "ok": True and "results", a list ordered best-match first.
        Each result has "company", "ticker", "fiscal_period", "section",
        "content_type" ("text" or "table"), "excerpt" (the matching passage; a
        table is given whole, with its units note and every row), "score" (0 to
        1, higher is closer), and "source_url". A table result also carries
        "units", "periods" and "rows" (label, kind, values as printed, and
        values_absolute in dollars or shares). An empty list means nothing
        matched.
    """
    hits = await _search_filings(query, company=company, section=section, k=max(1, min(k, 8)))
    results = []
    for hit in hits:
        entry: dict[str, Any] = {
            "company": hit.company,
            "ticker": hit.ticker,
            "fiscal_period": hit.fiscal_period,
            "section": hit.section,
            "content_type": hit.content_type,
            "score": round(hit.score, 4),
            "excerpt": _excerpt(hit.content, hit.content_type),
            "source_url": hit.source_url,
        }
        if hit.content_type == "table":
            structured = hit.structured or {}
            entry["units"] = structured.get("units")
            entry["periods"] = structured.get("periods")
            entry["rows"] = _rows(structured)
        results.append(entry)
    return ok(query=query, count=len(hits), results=results)


async def calculate_ratio(
    ticker: str,
    ratio_name: RatioName,
    period: Period = "annual",
    fiscal_year: int | None = None,
) -> dict[str, Any]:
    """Calculate a financial ratio for a company from its reported financials.

    Use this for questions about profitability, leverage, liquidity or
    valuation. The result includes the formula used and the raw dollar figures
    it was computed from, so the number can be checked rather than trusted.

    When the question names a fiscal year, pass it as fiscal_year. Without it
    the result is the most recent period of the kind asked for, named in
    "period_end", which may not be the year the question means. For a line
    item such as revenue, net income, EPS or a share count, use
    get_financial_fact instead.

    Args:
        ticker: Stock ticker symbol, e.g. "NVDA" or "AAPL". Case-insensitive.
        ratio_name: Which ratio to compute. One of:
            "gross_margin" - gross profit / revenue.
            "operating_margin" - operating income / revenue.
            "net_margin" - net income / revenue.
            "return_on_equity" - net income / shareholders' equity.
            "debt_to_equity" - total debt / shareholders' equity. Returned as a
                multiple, so 0.07 means debt is 7% of equity. Most recent
                fiscal year only.
            "current_ratio" - current assets / current liabilities.
            "pe_ratio" - share price / trailing twelve-month earnings per
                share. Always as-of-now, because it depends on the live market
                price; `period` and `fiscal_year` do not apply to it.
        period: Which reporting period to use when no fiscal_year is given.
            "annual" is the most recent completed fiscal year (default),
            "quarterly" the most recent reported quarter, "ttm" trailing
            twelve months.
        fiscal_year: The company's own fiscal year, e.g. 2025, for a ratio of
            that year. Fiscal years are the company's, not calendar: NVDA's
            FY2026 ended January 2026. Omit for the most recent period.

    Returns:
        On success, a dict with "ok": True and: ticker, ratio, period,
        period_end, value (margins and return_on_equity are fractions, so
        0.7107 means 71.07%; the others are multiples), formatted, formula,
        inputs, currency and source. With fiscal_year the result also carries
        "fiscal_year" and "accessions", the SEC filings its inputs came from.

        On failure, {"ok": False, "error": code, "message": ...}.
    """
    if fiscal_year is None or str(fiscal_year).strip() == "":
        return _latest_ratio(ticker, ratio_name, period)
    if (ratio_name or "").strip().lower() == "debt_to_equity":
        # Total debt is Yahoo's definition, which no single XBRL concept
        # matches, so this ratio has one source. It answers a fiscal-year
        # question only when that year is the one the source's latest annual
        # statement covers; the two disagree on the day (Yahoo normalises
        # NVIDIA's January 25 year end to January 31), not on the month.
        latest = _latest_ratio(ticker, ratio_name, "annual")
        ended = await fiscal_year_end(ticker, fiscal_year)
        if latest.get("ok") and ended and _same_year_end(latest.get("period_end"), ended):
            return {**latest, "fiscal_year": int(str(fiscal_year).upper().removeprefix("FY"))}
    return await ratio_for_fiscal_year(ticker, ratio_name, fiscal_year)


def _same_year_end(a: str | None, b: str | None) -> bool:
    """Whether two ISO dates are the same fiscal year end, give or take a week."""
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) <= 7
    except (TypeError, ValueError):
        return False


def build_registry() -> Registry:
    """The default tool set. One registry per process is plenty; it is stateless."""
    registry = Registry()
    # The aliases and the harmless value are what qwen2.5:7b sent on the full
    # eval of 2026-11-15: period="annual" and period_end=2026 to the facts
    # tool, each costing a turn on an error, and ticker="AAPL" to the filing
    # search, which dropped it and searched both companies.
    registry.register(get_financial_fact, reject_unknown=UNKNOWN_ARGUMENT_HINT,
                      aliases={"year": "fiscal_year", "period_end": "fiscal_year",
                               "company": "ticker"},
                      harmless={"period": ("annual", "fy", "year")})
    registry.register(search_filings, aliases={"ticker": "company"})
    registry.register(get_stock_price)
    registry.register(calculate_ratio)
    registry.register(search_news)
    return registry


# ─────────────────────────────────────────────────────────────────────────────
# Compaction: what the model sees of each result
# ─────────────────────────────────────────────────────────────────────────────

def compact_for_model(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Shrink a tool result to what is needed to answer with it.

    Errors are passed through untouched -- the message is the whole value of an
    error, and it is short.
    """
    if not result.get("ok"):
        return result

    if name == "get_stock_price":
        # The summary answers every price question a model asks. The daily bars
        # are 250 rows of six numbers and answer none of them on their own.
        compact = {key: result[key] for key in
                   ("ok", "ticker", "currency", "start_date", "end_date",
                    "trading_days", "first", "last", "high", "low",
                    "change", "change_pct") if key in result}
        bars = result.get("prices", [])
        if len(bars) <= MAX_PRICE_BARS:
            compact["prices"] = bars
        else:
            compact["prices_omitted"] = (
                f"{len(bars)} daily bars not shown; the summary above covers them."
            )
        return compact

    if name == "search_news":
        articles = result.get("articles", [])
        compact = {
            "ok": True,
            "query": result.get("query"),
            "days_back": result.get("days_back"),
            "count": result.get("count"),
            "headlines": [
                {"title": a["title"], "source": a["source"],
                 "published": (a["published"] or "")[:10]}
                for a in articles[:MAX_HEADLINES]
            ],
        }
        if len(articles) > MAX_HEADLINES:
            compact["note"] = (
                f"Showing {MAX_HEADLINES} of {len(articles)} headlines, newest first."
            )
        if result.get("coverage_note"):
            compact["coverage_note"] = result["coverage_note"]
        return compact

    if name == "search_filings":
        # The excerpt is whole for a table, so the model reads the statement as
        # rendered: caption, units note, aligned rows. The parsed `rows` stay in
        # the full result for the trace and the grounding guard; repeating every
        # figure a second time as JSON would cost prefill and tell the model
        # nothing the grid does not.
        return {
            "ok": True,
            "count": result.get("count"),
            "results": [
                {key: passage[key] for key in
                 ("ticker", "fiscal_period", "section", "content_type", "score", "excerpt")
                 if key in passage}
                for passage in result.get("results", [])
            ],
        }

    # calculate_ratio is already compact, and every field in it is load-bearing:
    # the value, the formula and the inputs are what make the number checkable.
    return result
