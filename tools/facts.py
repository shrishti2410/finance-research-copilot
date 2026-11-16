"""Reported financial facts by company and fiscal year, from SEC XBRL data.

    await get_financial_fact("NVDA", "revenue", 2026)
    await ratio_for_fiscal_year("AAPL", "gross_margin", 2025)

The rows come from SEC's company-facts API and live in the `xbrl_facts` table
(`db.models.XbrlFact`), filled by `scripts/index_facts.py`. A question about a
reported number -- "NVIDIA's diluted EPS in fiscal 2024" -- is then a lookup by
company, concept and fiscal year. Before this the agent had to retrieve the
income statement and read the right cell of a rendered grid, and the eval runs
of 2026-11-10 to 2026-11-15 show it picking the wrong year's column, the wrong
row, or the latest-period ratio tool instead.

A named fact, not a concept
---------------------------
XBRL concepts are exact and unfriendly: revenue is `Revenues` for NVIDIA since
fiscal 2023, `RevenueFromContractWithCustomerExcludingAssessedTax` for Apple,
and `SalesRevenueNet` for both further back. The tool takes a short name and
`FACTS` lists, in order of preference, the concepts that mean it. The first
one the company reported for that year is returned, and the result says which
concept it was, so the mapping is visible rather than trusted.

Every result carries the accession number of the filing the figure was taken
from. That is the citation: a reader can open the filing and find the number.

A result also says what it is not
---------------------------------
A fast-subset run on 2026-11-15 showed the cost of an exact lookup that is
nearly what was asked. "What did Apple raise its quarterly dividend to?" got
the year's total dividends per share, $1.02, stated as the answer where the
agent had previously declined. "How much was NVIDIA authorized to repurchase?"
got the cash it spent on buybacks. A question about Data Center revenue was
sent with a `segment` argument, which the registry drops, and came back as
total revenue. Wording alone did not hold: with a `scope` line on the result
and the fact described as "not the quarterly dividend rate", the dividend
question was still answered with $1.02, now with the claim that Apple had not
raised its dividend. So:

- Dividends per share, dividends paid and share repurchases are **not** named
  facts. They are real XBRL figures, but each is one word away from a question
  this tool cannot answer, and a model that finds a number stops looking.
  Asking for one is an unknown fact, and the error points to search_filings.
- The tool is registered to reject arguments it does not have, so `segment`
  is an error that says there is no segment figure here, not a silent drop.
- Every result still carries the `scope` line.

The full Qwen run of 2026-11-15 found one more of the same kind. `gross_margin`
was accepted as a spelling of `gross_profit`, because both companies label the
dollar line "gross margin". Asked "What was NVIDIA's annual gross margin for
fiscal 2026?", the model called this tool with fact="gross_margin" and
answered "$153.463 billion" where the ratio tool gives 71.07%.

Refusing the name was tried first and was not enough for the 7B model: told
"that is a ratio, call calculate_ratio", it answered from memory in two of
three re-asked questions and was only pulled back by the grounding guard, and
"gross margin in dollars", which had passed, was lost. So a ratio name is
answered here with the ratio for that fiscal year, computed as calculate_ratio
computes it, and a `note` that states both readings with their labels: the
percentage, and the dollar figure it was computed from. One call, and neither
reading has to be guessed. Ratios with no fiscal-year form (pe_ratio,
debt_to_equity) are still an error that says where to go.

What is not here
----------------
Segment figures. Company-facts holds only numbers reported for the whole
entity, so Apple's Services net sales and NVIDIA's Data Center revenue are not
in it. Those stay with `search_filings`.
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from db.base import engine
from db.models import XbrlFact
from tools.base import ERROR_BAD_INPUT, ERROR_NO_DATA, ERROR_UNSUPPORTED, error, ok

__all__ = [
    "FACTS", "FactName", "get_financial_fact", "ratio_for_fiscal_year",
    "fiscal_year_end", "upsert_facts", "FACT_RATIOS", "UNKNOWN_ARGUMENT_HINT",
]

# name -> (what it is, concepts that mean it in order of preference).
#
# Order matters where a company has used more than one over the years: the
# first concept with a value for the requested year wins.
FACTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "revenue": ("Total revenue (net sales)", (
        "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet")),
    "cost_of_revenue": ("Cost of revenue (cost of sales)", (
        "CostOfRevenue", "CostOfGoodsAndServicesSold")),
    "gross_profit": ("Gross profit (gross margin in dollars)", ("GrossProfit",)),
    "research_and_development": ("Research and development expense", (
        "ResearchAndDevelopmentExpense",)),
    "selling_general_and_administrative": ("Selling, general and administrative expense", (
        "SellingGeneralAndAdministrativeExpense",)),
    "operating_expenses": ("Total operating expenses", ("OperatingExpenses",)),
    "operating_income": ("Operating income", ("OperatingIncomeLoss",)),
    "pretax_income": ("Income before income taxes", (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    )),
    "income_tax_expense": ("Income tax expense (provision for income taxes)", (
        "IncomeTaxExpenseBenefit",)),
    "net_income": ("Net income", ("NetIncomeLoss",)),
    "eps_basic": ("Basic earnings per share", ("EarningsPerShareBasic",)),
    "eps_diluted": ("Diluted earnings per share", ("EarningsPerShareDiluted",)),
    "shares_basic": ("Weighted-average basic shares outstanding", (
        "WeightedAverageNumberOfSharesOutstandingBasic",)),
    "shares_diluted": ("Weighted-average diluted shares outstanding", (
        "WeightedAverageNumberOfDilutedSharesOutstanding",)),
    "operating_cash_flow": ("Net cash provided by operating activities", (
        "NetCashProvidedByUsedInOperatingActivities",)),
    "capital_expenditures": ("Payments for property, plant and equipment", (
        "PaymentsToAcquirePropertyPlantAndEquipment",)),
    "total_assets": ("Total assets, at fiscal year end", ("Assets",)),
    "total_liabilities": ("Total liabilities, at fiscal year end", ("Liabilities",)),
    "shareholders_equity": ("Total shareholders' equity, at fiscal year end", (
        "StockholdersEquity",)),
    "current_assets": ("Total current assets, at fiscal year end", ("AssetsCurrent",)),
    "current_liabilities": ("Total current liabilities, at fiscal year end", (
        "LiabilitiesCurrent",)),
    "cash_and_equivalents": ("Cash and cash equivalents, at fiscal year end", (
        "CashAndCashEquivalentsAtCarryingValue",)),
    "long_term_debt": ("Long-term debt including the current portion, at fiscal year end", (
        "LongTermDebt",)),
}

FactName = Literal[
    "revenue", "cost_of_revenue", "gross_profit", "research_and_development",
    "selling_general_and_administrative", "operating_expenses", "operating_income",
    "pretax_income", "income_tax_expense", "net_income", "eps_basic", "eps_diluted",
    "shares_basic", "shares_diluted", "operating_cash_flow", "capital_expenditures",
    "total_assets",
    "total_liabilities", "shareholders_equity", "current_assets", "current_liabilities",
    "cash_and_equivalents", "long_term_debt",
]

# What a model is likely to say instead of the name above.
_ALIASES = {
    "revenues": "revenue", "total_revenue": "revenue", "net_sales": "revenue", "sales": "revenue",
    "cost_of_sales": "cost_of_revenue", "cogs": "cost_of_revenue",
    "rnd": "research_and_development", "r&d": "research_and_development",
    "research_and_development_expense": "research_and_development",
    "sga": "selling_general_and_administrative", "sg&a": "selling_general_and_administrative",
    "operating_profit": "operating_income",
    "income_before_tax": "pretax_income", "income_before_income_taxes": "pretax_income",
    "pre_tax_income": "pretax_income",
    "tax_expense": "income_tax_expense", "income_taxes": "income_tax_expense",
    "provision_for_income_taxes": "income_tax_expense",
    "net_profit": "net_income", "earnings": "net_income",
    "basic_eps": "eps_basic", "diluted_eps": "eps_diluted", "eps": "eps_diluted",
    "basic_shares": "shares_basic", "diluted_shares": "shares_diluted",
    "weighted_average_shares_basic": "shares_basic",
    "weighted_average_shares_diluted": "shares_diluted",
    "capex": "capital_expenditures",
    "cash_from_operations": "operating_cash_flow", "assets": "total_assets",
    "liabilities": "total_liabilities", "equity": "shareholders_equity",
    "stockholders_equity": "shareholders_equity", "cash": "cash_and_equivalents",
    "debt": "long_term_debt",
}

_FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/"

# Carried on every result. See the module docstring.
SCOPE = ("Whole company, whole fiscal year. Not a segment, product or quarter: "
         "those are in the filing text, via search_filings.")

# Given to the registry: what to say when the tool is called with an argument
# it does not have. See the module docstring.
UNKNOWN_ARGUMENT_HINT = (
    "Every figure here is for the whole company and the whole fiscal year: there is "
    "no segment, product or quarter argument. For a segment or product figure, use "
    "search_filings."
)


# Ratio names a model sends here by mistake -> the calculate_ratio name, and
# the fact that is the same words in dollars, if there is one. See the module
# docstring: these are answered as the ratio, with both readings labelled.
_RATIO_NAMES: dict[str, tuple[str, str | None]] = {
    "gross_margin": ("gross_margin", "gross_profit"),
    "gross_margin_percentage": ("gross_margin", "gross_profit"),
    "gross_margin_pct": ("gross_margin", "gross_profit"),
    "gross_profit_margin": ("gross_margin", "gross_profit"),
    "operating_margin": ("operating_margin", "operating_income"),
    "net_margin": ("net_margin", "net_income"),
    "net_profit_margin": ("net_margin", "net_income"),
    "profit_margin": ("net_margin", "net_income"),
    "return_on_equity": ("return_on_equity", None),
    "roe": ("return_on_equity", None),
    "current_ratio": ("current_ratio", None),
    "debt_to_equity": ("debt_to_equity", None),
    "pe_ratio": ("pe_ratio", None),
    "p/e": ("pe_ratio", None),
}


def _normalized(raw: str) -> str:
    return (raw or "").strip().lower().replace("-", "_").replace(" ", "_")


def _fact_name(raw: str) -> str | None:
    name = _normalized(raw)
    name = _ALIASES.get(name, name)
    return name if name in FACTS else None


def _fiscal_year(raw) -> int | None:
    """A fiscal year from whatever the model sent: 2026, "2026", "FY2026"."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw or "").strip().upper().removeprefix("FY").strip()
    return int(text) if text.isdigit() else None


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────

async def upsert_facts(conn, rows) -> int:
    """Insert or replace fact rows. Returns how many were written.

    Keyed on the table's unique constraint, so indexing a company again
    replaces its figures -- a restated number overwrites the old one -- and
    never duplicates them.
    """
    payload = [
        {
            "cik": r.cik, "ticker": r.ticker, "taxonomy": r.taxonomy, "concept": r.concept,
            "unit": r.unit, "fiscal_year": r.fiscal_year, "fiscal_period": r.fiscal_period,
            "period_start": r.period_start, "period_end": r.period_end, "value": r.value,
            "accession": r.accession, "form": r.form, "filed": r.filed,
        }
        for r in rows
    ]
    written = 0
    # Thirteen bind parameters a row; batches keep one statement well under the
    # driver's 32,767-parameter limit.
    for start in range(0, len(payload), 1000):
        batch = payload[start:start + 1000]
        statement = insert(XbrlFact).values(batch)
        statement = statement.on_conflict_do_update(
            constraint="uq_xbrl_facts_company_concept_period",
            set_={column: statement.excluded[column] for column in
                  ("ticker", "period_start", "period_end", "value", "accession", "form", "filed")},
        )
        await conn.execute(statement)
        written += len(batch)
    return written


async def _lookup(conn, ticker: str, concepts: tuple[str, ...], fiscal_year: int):
    """The row for the first of `concepts` the company reported that year, or None."""
    rows = (await conn.execute(
        select(XbrlFact).where(
            XbrlFact.ticker == ticker, XbrlFact.fiscal_year == fiscal_year,
            XbrlFact.fiscal_period == "FY", XbrlFact.concept.in_(concepts),
        )
    )).all()
    by_concept: dict[str, list] = {}
    for row in rows:
        by_concept.setdefault(row.concept, []).append(row)
    for concept in concepts:
        candidates = by_concept.get(concept)
        if candidates:
            # A concept reported in two units is rare; prefer the plain ones.
            candidates.sort(key=lambda r: (r.unit not in ("USD", "shares", "USD/shares"), r.unit))
            return candidates[0]
    return None


async def _years(conn, ticker: str, concepts: tuple[str, ...] | None = None) -> list[int]:
    query = select(XbrlFact.fiscal_year).where(
        XbrlFact.ticker == ticker, XbrlFact.fiscal_period == "FY").distinct()
    if concepts:
        query = query.where(XbrlFact.concept.in_(concepts))
    return sorted(row[0] for row in (await conn.execute(query)).all())


def _number(value) -> float | int:
    """A NUMERIC as a plain number: 215938000000, not Decimal('215938000000.000000')."""
    as_float = float(value)
    return int(as_float) if as_float.is_integer() and abs(as_float) >= 1000 else as_float


def _formatted(value: float, unit: str) -> str:
    if unit == "USD/shares":
        return f"${value:,.2f} per share" if abs(value) >= 0.1 else f"${value:,.3f} per share"
    if unit == "shares":
        return f"{value:,.0f} shares"
    if unit == "USD":
        if abs(value) >= 1e9:
            return f"${value / 1e9:,.3f} billion"
        if abs(value) >= 1e6:
            return f"${value / 1e6:,.1f} million"
        return f"${value:,.0f}"
    return f"{value:,} {unit}"


def _fact_fields(row) -> dict[str, Any]:
    value = _number(row.value)
    return {
        "value": value,
        "unit": row.unit,
        "formatted": _formatted(float(row.value), row.unit),
        "period_start": row.period_start.isoformat() if row.period_start else None,
        "period_end": row.period_end.isoformat(),
        "concept": f"{row.taxonomy}:{row.concept}",
        "accession": row.accession,
        "form": row.form,
        "filed": row.filed.isoformat(),
        "source_url": _FILING_URL.format(
            cik_int=int(row.cik), accession_nodash=row.accession.replace("-", "")),
    }


async def _not_found(conn, symbol: str, year: int, concepts, what: str) -> dict[str, Any]:
    """Why a lookup found nothing: company not ingested, or that year not reported."""
    all_years = await _years(conn, symbol)
    if not all_years:
        return error(
            ERROR_NO_DATA,
            f"No structured facts are stored for {symbol!r}. Facts are ingested per "
            f"company; this one has not been. Use search_filings or calculate_ratio.",
            ticker=symbol,
        )
    years = await _years(conn, symbol, concepts)
    if not years:
        return error(
            ERROR_NO_DATA,
            f"{symbol} does not report {what} as a company-wide XBRL fact in its annual "
            f"reports. If it is a segment or product figure, use search_filings.",
            ticker=symbol, fiscal_year=year,
        )
    return error(
        ERROR_NO_DATA,
        f"{symbol} has no {what} for fiscal year {year}. Fiscal years available: "
        f"{', '.join(str(y) for y in years[-12:])}. A fiscal year is the company's own "
        f"label: NVIDIA's fiscal 2026 ended in January 2026.",
        ticker=symbol, fiscal_year=year, available_fiscal_years=years[-12:],
    )


async def fiscal_year_end(ticker: str, fiscal_year) -> str | None:
    """The ISO date a company's fiscal year ended, or None if it is not stored."""
    year = _fiscal_year(fiscal_year)
    if year is None:
        return None
    async with engine.connect() as conn:
        row = (await conn.execute(
            select(XbrlFact.period_end).where(
                XbrlFact.ticker == (ticker or "").strip().upper(),
                XbrlFact.fiscal_year == year, XbrlFact.fiscal_period == "FY",
            ).limit(1)
        )).first()
    return row[0].isoformat() if row else None


# ─────────────────────────────────────────────────────────────────────────────
# The tool
# ─────────────────────────────────────────────────────────────────────────────

async def get_financial_fact(ticker: str, fact: FactName, fiscal_year: int) -> dict[str, Any]:
    """Get one reported financial figure for a company and fiscal year.

    Use this first for any reported number for a named fiscal year: revenue,
    net income, EPS, share counts, tax expense, operating income, cash flow,
    balance-sheet totals. It is an exact lookup in SEC's XBRL data, and the
    result cites the filing it came from. For a ratio, use calculate_ratio.

    Every figure is for the whole company and the whole fiscal year. It has no
    segment or product argument. Use search_filings instead for segment and
    product figures (Apple's Services net sales, NVIDIA's Data Center
    revenue), for dividends and share repurchases, for anything announced
    on a date, and for anything a company said in words.

    Args:
        ticker: Stock ticker symbol, e.g. "NVDA" or "AAPL".
        fact: Which figure. "pretax_income" is income before income taxes;
            "shares_basic" and "shares_diluted" are the weighted-average share
            counts used for EPS; balance-sheet figures are at fiscal year end.
        fiscal_year: The company's own fiscal year, e.g. 2026. NVIDIA's fiscal
            2026 ended January 2026; Apple's fiscal 2025 ended September 2025.

    Returns:
        On success, a dict with "ok": True and:
            ticker, fact, fiscal_year: what was asked for, normalized.
            description, scope: what the figure is, and that it covers the
                whole company for the whole fiscal year.
            value: the figure, in `unit` ("USD", "shares" or "USD/shares").
                Dollar amounts are in dollars, not millions.
            formatted: the same figure rendered for a human.
            period_start, period_end: ISO dates the figure covers; period_start
                is null for a balance on a day.
            concept: the XBRL concept it was reported under.
            accession: the SEC accession number of the filing it was taken
                from. Cite it. "form" and "filed" describe that filing and
                "source_url" is its folder on sec.gov.

        On failure, {"ok": False, "error": code, "message": ...} where code is
        "bad_input" (no ticker, unknown fact name, or a fiscal year that is not
        a year) or "no_data" (the company is not ingested, does not report the
        figure, or has none for that year -- the message lists the years it has).
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return error(ERROR_BAD_INPUT, "ticker must be a non-empty symbol like 'NVDA'.")
    name = _fact_name(fact)
    if name is None and _normalized(fact) in _RATIO_NAMES:
        ratio, dollars = _RATIO_NAMES[_normalized(fact)]
        result = await ratio_for_fiscal_year(symbol, ratio, fiscal_year)
        if not result.get("ok"):
            return result
        note = (f"{ratio} is a ratio, not a reported figure: {result['formatted']} for "
                f"fiscal {result['fiscal_year']}, computed as {result['formula']}.")
        if dollars:
            amount = _formatted(float(result["inputs"][dollars]), "USD")
            note += (f" The same words in dollars are the fact {dollars!r}: {amount} "
                     f"for fiscal {result['fiscal_year']}.")
        return {**result, "asked_for": fact, "note": note, "scope": SCOPE}
    if name is None:
        return error(
            ERROR_BAD_INPUT,
            f"Unknown fact {fact!r}. Known facts: {', '.join(FACTS)}. Segment and "
            f"product figures, dividends and share repurchases are not structured "
            f"facts: use search_filings.",
            ticker=symbol,
        )
    year = _fiscal_year(fiscal_year)
    if year is None or not (1990 <= year <= 2100):
        return error(
            ERROR_BAD_INPUT,
            f"fiscal_year must be a year such as 2026, got {fiscal_year!r}.",
            ticker=symbol, fact=name,
        )

    description, concepts = FACTS[name]
    async with engine.connect() as conn:
        row = await _lookup(conn, symbol, concepts, year)
        if row is None:
            return await _not_found(conn, symbol, year, concepts, description.lower())
    return ok(ticker=symbol, fact=name, description=description, scope=SCOPE,
              fiscal_year=year, **_fact_fields(row))


# ─────────────────────────────────────────────────────────────────────────────
# Ratios for a named fiscal year
# ─────────────────────────────────────────────────────────────────────────────

# ratio -> (numerator fact, denominator fact, formula). The same formulas as
# tools/ratios.py, over the company's own reported figures.
FACT_RATIOS: dict[str, tuple[str, str, str]] = {
    "gross_margin": ("gross_profit", "revenue", "gross_profit / revenue"),
    "operating_margin": ("operating_income", "revenue", "operating_income / revenue"),
    "net_margin": ("net_income", "revenue", "net_income / revenue"),
    "return_on_equity": ("net_income", "shareholders_equity", "net_income / shareholders_equity"),
    "current_ratio": ("current_assets", "current_liabilities",
                      "current_assets / current_liabilities"),
}
_PERCENT_RATIOS = ("gross_margin", "operating_margin", "net_margin", "return_on_equity")


async def ratio_for_fiscal_year(ticker: str, ratio_name: str, fiscal_year) -> dict[str, Any]:
    """A ratio for one named fiscal year, from the company's reported facts.

    Returns the same shape as `tools.ratios.calculate_ratio`, plus the
    accession numbers its inputs came from.
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return error(ERROR_BAD_INPUT, "ticker must be a non-empty symbol like 'NVDA'.")
    ratio = (ratio_name or "").strip().lower()
    year = _fiscal_year(fiscal_year)
    if year is None or not (1990 <= year <= 2100):
        return error(ERROR_BAD_INPUT,
                     f"fiscal_year must be a year such as 2026, got {fiscal_year!r}.",
                     ticker=symbol)
    if ratio == "pe_ratio":
        return error(
            ERROR_UNSUPPORTED,
            "pe_ratio depends on a share price, so it has no fiscal-year form here. For a "
            "P/E at a past date, divide that day's close from get_stock_price by the "
            "diluted EPS from get_financial_fact(fact=\"eps_diluted\").",
            ticker=symbol, ratio=ratio, fiscal_year=year,
        )
    if ratio == "debt_to_equity":
        return error(
            ERROR_UNSUPPORTED,
            f"debt_to_equity is available for the most recent fiscal year only, and "
            f"fiscal {year} is not it. Total debt is not one reported XBRL figure. For "
            f"this year, get_financial_fact gives long_term_debt and shareholders_equity.",
            ticker=symbol, ratio=ratio, fiscal_year=year,
        )
    if ratio not in FACT_RATIOS:
        return error(
            ERROR_UNSUPPORTED,
            f"Unknown ratio {ratio_name!r}. With a fiscal year: {', '.join(FACT_RATIOS)}.",
            ticker=symbol,
        )

    top_name, bottom_name, formula = FACT_RATIOS[ratio]
    async with engine.connect() as conn:
        top = await _lookup(conn, symbol, FACTS[top_name][1], year)
        bottom = await _lookup(conn, symbol, FACTS[bottom_name][1], year)
        for name, row in ((top_name, top), (bottom_name, bottom)):
            if row is None:
                return await _not_found(conn, symbol, year, FACTS[name][1],
                                        FACTS[name][0].lower())

    numerator, denominator = float(top.value), float(bottom.value)
    if denominator == 0:
        return error(ERROR_NO_DATA,
                     f"{symbol}'s {bottom_name} is zero for fiscal {year}, so {ratio} is undefined.",
                     ticker=symbol, ratio=ratio, fiscal_year=year)
    value = numerator / denominator
    return ok(
        ticker=symbol, ratio=ratio, period="annual", fiscal_year=year,
        period_end=top.period_end.isoformat(),
        value=round(value, 6),
        formatted=(f"{value * 100:.2f}%" if ratio in _PERCENT_RATIOS else f"{value:.2f}x"),
        formula=formula,
        inputs={top_name: _number(top.value), bottom_name: _number(bottom.value)},
        currency="USD" if top.unit == "USD" else top.unit,
        source="SEC XBRL company facts",
        accessions=sorted({top.accession, bottom.accession}),
    )
