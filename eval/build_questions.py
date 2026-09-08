"""Regenerate eval/questions.json from real sources.

    python -m eval.build_questions

Run this when the corpus is re-ingested or the price windows go stale.

Not one expected answer is typed in here. Line items are read out of the parsed
tables already in Postgres, tool answers come from calling the tools, and
derived figures are computed from those two. A dataset whose ground truth was
written from memory measures the memory, not the system.

Every case carries a `source` string naming where its number came from, so a
failing case can be argued with.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402

from db.base import SessionLocal, engine  # noqa: E402
from tools.ratios import calculate_ratio  # noqa: E402
from tools.stock_price import get_stock_price  # noqa: E402

OUT = Path(__file__).with_name("questions.json")

# Column order in each stored table, newest first.
NVDA_PERIODS = ["FY2026", "FY2025", "FY2024"]
AAPL_PERIODS = ["FY2025", "FY2024", "FY2023"]

# Price windows. Closed and in the past, so the answers do not move -- except
# for dividend re-adjustment, which is why price tolerances are 2%.
WINDOWS = {
    "nvda_aug": ("NVDA", "2026-08-03", "2026-08-07"),
    "aapl_aug": ("AAPL", "2026-08-03", "2026-08-07"),
    "nvda_jul": ("NVDA", "2026-07-01", "2026-07-31"),
    "aapl_jun": ("AAPL", "2026-06-01", "2026-06-30"),
}


async def read_tables() -> dict:
    """{ticker: {label: [values newest-first]}} from the stored statements."""
    async with SessionLocal() as session:
        rows = (await session.execute(text(
            "SELECT ticker, structured FROM filing_chunks WHERE content_type='table'"
        ))).mappings().all()
    await engine.dispose()   # on the loop that opened them, not a later one
    tables = {}
    for row in rows:
        structured = row["structured"]
        if isinstance(structured, str):
            structured = json.loads(structured)
        by_label = {}
        for entry in structured.get("rows", []):
            values = entry.get("values_absolute") or []
            if entry.get("kind") == "heading" or not values:
                continue
            # Labels repeat across sections ("Products" under both net sales
            # and cost of sales); the first occurrence wins and the second is
            # reached by a qualified key below.
            by_label.setdefault(entry["label"], values)
            by_label[f"{entry['label']}#{len([k for k in by_label if k.startswith(entry['label'])])}"] = values
        tables[row["ticker"]] = by_label
    return tables


def millions(value: float) -> float:
    return round(value / 1_000_000, 1)


def billions(value: float) -> float:
    return round(value / 1_000_000_000, 2)


DROPPED = {
    # Redundant with other line items from the same statement.
    "aapl-tax-provision-fy2025",
    "aapl-opex-fy2025",
    "nvda-tax-expense-fy2026",
    # Redundant with the buyback-authorisation and buyback-program cases.
    "nvda-commercial-paper",
    "aapl-buyback-actual",
    # Redundant with the July high.
    "nvda-july-low",
    # Two balance-sheet-only cases are enough to make the point.
    "nvda-current-ratio-tool",
    # Reading the right year is already covered by nvda-revenue-fy2024.
    "nvda-gross-margin-pct-prior-mdna",
    # Covered by the derived operating-margin case.
    "nvda-operating-income-fy2026",
}

def build() -> list[dict]:
    tables = asyncio.run(read_tables())
    nvda, aapl = tables["NVDA"], tables["AAPL"]

    prices = {key: get_stock_price(*spec) for key, spec in WINDOWS.items()}
    for key, data in prices.items():
        if not data["ok"]:
            raise SystemExit(f"price window {key} failed: {data.get('message')}")

    ratio = {
        (t, r): calculate_ratio(t, r, "annual")
        for t in ("NVDA", "AAPL")
        for r in ("gross_margin", "operating_margin", "net_margin",
                  "debt_to_equity", "current_ratio")
    }
    for key, data in ratio.items():
        if not data["ok"]:
            raise SystemExit(f"ratio {key} failed: {data.get('message')}")

    def nv(label: str, period: str = "FY2026") -> float:
        return nvda[label][NVDA_PERIODS.index(period)]

    def ap(label: str, period: str = "FY2025") -> float:
        return aapl[label][AAPL_PERIODS.index(period)]

    cases: list[dict] = []

    def add(id, question, expected, unit, tolerance, via, company, source, notes=None):
        case = {
            "id": id,
            "question": question,
            "expected_answer": expected,
            "unit": unit,
            "tolerance": tolerance,
            "answerable_via": via,
            "company": company,
            "source": source,
        }
        if notes:
            case["notes"] = notes
        if id not in DROPPED:
            cases.append(case)

    M = "USD millions"
    B = "USD billions"
    PCT = "percent"
    USD = "USD"
    X = "ratio (x)"
    FIL = ["search_filings"]
    RAT = ["calculate_ratio"]
    PRI = ["get_stock_price"]

    # ── filings: line items read straight off the statement ─────────────────
    add("nvda-revenue-fy2026",
        "What was NVIDIA's total revenue for fiscal year 2026?",
        millions(nv("Revenue")), M, 1.0, FIL, "NVDA",
        "NVDA FY2026 income statement, 'Revenue', column Jan 25, 2026")

    add("nvda-gross-profit-fy2026",
        "What was NVIDIA's gross profit in fiscal year 2026?",
        millions(nv("Gross profit")), M, 1.0, FIL, "NVDA",
        "NVDA FY2026 income statement, 'Gross profit'")

    add("nvda-rnd-fy2026",
        "How much did NVIDIA spend on research and development in fiscal 2026?",
        millions(nv("Research and development")), M, 1.0, FIL, "NVDA",
        "NVDA FY2026 income statement, 'Research and development'")

    add("nvda-operating-income-fy2026",
        "What was NVIDIA's operating income for fiscal year 2026?",
        millions(nv("Operating income")), M, 1.0, FIL, "NVDA",
        "NVDA FY2026 income statement, 'Operating income'")

    add("nvda-net-income-fy2026",
        "What was NVIDIA's net income in fiscal year 2026?",
        millions(nv("Net income")), M, 1.0, FIL, "NVDA",
        "NVDA FY2026 income statement, 'Net income'")

    add("nvda-tax-expense-fy2026",
        "What was NVIDIA's income tax expense for fiscal year 2026?",
        millions(nv("Income tax expense")), M, 1.0, FIL, "NVDA",
        "NVDA FY2026 income statement, 'Income tax expense'")

    add("nvda-diluted-eps-fy2026",
        "What was NVIDIA's diluted earnings per share in fiscal year 2026?",
        nv("Diluted"), USD, 0.01, FIL, "NVDA",
        "NVDA FY2026 income statement, diluted net income per share",
        "Per-share rows are not scaled to millions; this case pins that.")

    add("nvda-revenue-fy2024",
        "What was NVIDIA's revenue in fiscal year 2024?",
        millions(nv("Revenue", "FY2024")), M, 1.0, FIL, "NVDA",
        "NVDA FY2026 income statement, 'Revenue', column Jan 28, 2024",
        "A prior-year column in the same table: the retrieved chunk holds "
        "three years, so this tests reading the right one.")

    add("nvda-diluted-shares-fy2026",
        "What was NVIDIA's weighted average diluted share count in fiscal 2026?",
        millions(nv("Diluted#2")), "millions of shares", 50.0, FIL, "NVDA",
        "NVDA FY2026 income statement, diluted weighted average shares",
        "Share-count rows carry their own scale; this pins that too.")

    add("aapl-revenue-fy2025",
        "What were Apple's total net sales for fiscal year 2025?",
        millions(ap("Total net sales")), M, 1.0, FIL, "AAPL",
        "AAPL FY2025 income statement, 'Total net sales'")

    add("aapl-services-fy2025",
        "How much revenue did Apple's Services segment generate in fiscal 2025?",
        millions(ap("Services")), M, 1.0, FIL, "AAPL",
        "AAPL FY2025 income statement, 'Services' under Net sales")

    add("aapl-gross-margin-dollars-fy2025",
        "What was Apple's gross margin in dollars for fiscal year 2025?",
        millions(ap("Gross margin")), M, 1.0, FIL, "AAPL",
        "AAPL FY2025 income statement, 'Gross margin'",
        "Apple labels the dollar figure 'Gross margin', not 'Gross profit'.")

    add("aapl-net-income-fy2025",
        "What was Apple's net income in fiscal year 2025?",
        millions(ap("Net income")), M, 1.0, FIL, "AAPL",
        "AAPL FY2025 income statement, 'Net income'")

    add("aapl-basic-eps-fy2025",
        "What was Apple's basic earnings per share in fiscal year 2025?",
        ap("Basic"), USD, 0.01, FIL, "AAPL",
        "AAPL FY2025 income statement, basic EPS",
        "The units string scales amounts to millions and shares to thousands "
        "while EPS is unscaled; this is the case that catches getting that wrong.")

    add("aapl-basic-shares-fy2025",
        "What was Apple's weighted average basic share count in fiscal 2025?",
        billions(ap("Basic#2")), "billions of shares", 0.05, FIL, "AAPL",
        "AAPL FY2025 income statement, basic weighted average shares",
        "Reflected in thousands in the source table.")

    add("aapl-net-income-fy2023",
        "What was Apple's net income in fiscal year 2023?",
        millions(ap("Net income", "FY2023")), M, 1.0, FIL, "AAPL",
        "AAPL FY2025 income statement, 'Net income', column September 30, 2023")

    add("aapl-tax-provision-fy2025",
        "What was Apple's provision for income taxes in fiscal 2025?",
        millions(ap("Provision for income taxes")), M, 1.0, FIL, "AAPL",
        "AAPL FY2025 income statement, 'Provision for income taxes'")

    # ── filings: facts stated in MD&A prose, not in the table ────────────────
    add("nvda-gross-margin-pct-mdna",
        "According to NVIDIA's MD&A, what was its gross margin percentage in "
        "fiscal year 2026?",
        71.1, PCT, 0.5, FIL, "NVDA",
        "NVDA Item 7: 'Gross margins decreased to 71.1% in fiscal year 2026 "
        "from 75.0% in fiscal year 2025'")

    add("nvda-gross-margin-pct-prior-mdna",
        "What gross margin percentage did NVIDIA report for fiscal year 2025?",
        75.0, PCT, 0.5, FIL, "NVDA",
        "NVDA Item 7, same sentence as above")

    add("nvda-effective-tax-rate-mdna",
        "What was NVIDIA's income tax expense as a percentage of income before "
        "income tax in fiscal 2026?",
        15.1, PCT, 0.5, FIL, "NVDA",
        "NVDA Item 7: 'Income tax as a percentage of income before income tax "
        "was an expense of 15.1% and 13.3%'")

    add("nvda-buyback-authorization",
        "As of January 25, 2026, how much was NVIDIA authorized to spend "
        "repurchasing its own stock?",
        58.5, B, 0.5, FIL, "NVDA",
        "NVDA Item 7: 'authorized ... to repurchase up to $58.5 billion'")

    add("nvda-datacenter-growth",
        "By what percentage did NVIDIA's Data Center revenue grow in fiscal 2026?",
        68.0, PCT, 1.0, FIL, "NVDA",
        "NVDA Item 7: 'Data Center revenue for fiscal year 2026 was up 68%'")

    add("nvda-commercial-paper",
        "How large is NVIDIA's commercial paper program as increased in "
        "January 2026?",
        25.0, B, 0.5, FIL, "NVDA",
        "NVDA Item 7: 'up to $25.0 billion'")

    add("aapl-buyback-program",
        "How large was the share repurchase program Apple announced in May 2025?",
        100.0, B, 1.0, FIL, "AAPL",
        "AAPL Item 7: 'a new share repurchase program of up to $100 billion'")

    add("aapl-dividend-per-share",
        "What did Apple raise its quarterly dividend to in May 2025?",
        0.26, USD, 0.005, FIL, "AAPL",
        "AAPL Item 7: 'raised its quarterly dividend from $0.25 to $0.26 per share'")

    add("aapl-buyback-actual",
        "How much of its own stock did Apple actually repurchase during fiscal 2025?",
        89.3, B, 0.5, FIL, "AAPL",
        "AAPL Item 7: 'the Company repurchased $89.3 billion of its common stock'")

    # ── filings + arithmetic over the retrieved statement ────────────────────
    add("nvda-operating-margin-derived",
        "What was NVIDIA's operating margin in fiscal year 2026?",
        round(nv("Operating income") / nv("Revenue") * 100, 2), PCT, 0.5,
        ["search_filings", "calculate_ratio"], "NVDA",
        "Computed: operating income / revenue from the NVDA FY2026 statement; "
        "calculate_ratio('NVDA','operating_margin') independently gives "
        f"{ratio[('NVDA','operating_margin')]['formatted']}",
        "Reachable either by retrieving the statement and dividing, or by the "
        "ratio tool. Both routes are correct.")

    add("nvda-effective-tax-rate-derived",
        "What effective tax rate did NVIDIA pay in fiscal 2026, based on its "
        "income statement?",
        round(nv("Income tax expense") / nv("Income before income tax") * 100, 2),
        PCT, 0.5, FIL, "NVDA",
        "Computed: income tax expense / income before income tax, NVDA FY2026")

    add("nvda-revenue-growth-derived",
        "By what percentage did NVIDIA's total revenue grow from fiscal 2025 "
        "to fiscal 2026?",
        round((nv("Revenue") / nv("Revenue", "FY2025") - 1) * 100, 2), PCT, 1.0,
        FIL, "NVDA",
        "Computed from the two revenue columns; MD&A states 'up 65%'")

    add("aapl-gross-margin-pct-derived",
        "What was Apple's gross margin percentage in fiscal year 2025?",
        round(ap("Gross margin") / ap("Total net sales") * 100, 2), PCT, 0.5,
        ["search_filings", "calculate_ratio"], "AAPL",
        "Computed: gross margin / total net sales, AAPL FY2025; "
        f"calculate_ratio gives {ratio[('AAPL','gross_margin')]['formatted']}")

    add("aapl-services-share-derived",
        "What share of Apple's fiscal 2025 revenue came from Services?",
        round(ap("Services") / ap("Total net sales") * 100, 2), PCT, 0.5,
        FIL, "AAPL",
        "Computed: Services / Total net sales, AAPL FY2025")

    add("aapl-opex-fy2025",
        "What were Apple's total operating expenses in fiscal 2025?",
        millions(ap("Total operating expenses")), M, 1.0, FIL, "AAPL",
        "AAPL FY2025 income statement, 'Total operating expenses'")

    # ── tools: ratios ───────────────────────────────────────────────────────
    add("nvda-gross-margin-tool",
        "What is NVIDIA's most recent annual gross margin?",
        round(ratio[("NVDA", "gross_margin")]["value"] * 100, 2), PCT, 0.5,
        RAT, "NVDA",
        f"calculate_ratio('NVDA','gross_margin','annual') = "
        f"{ratio[('NVDA','gross_margin')]['formatted']}")

    add("aapl-gross-margin-tool",
        "What is Apple's most recent annual gross margin?",
        round(ratio[("AAPL", "gross_margin")]["value"] * 100, 2), PCT, 0.5,
        RAT, "AAPL",
        f"calculate_ratio('AAPL','gross_margin','annual') = "
        f"{ratio[('AAPL','gross_margin')]['formatted']}")

    add("nvda-debt-to-equity-tool",
        "What is NVIDIA's debt-to-equity ratio?",
        round(ratio[("NVDA", "debt_to_equity")]["value"], 2), X, 0.05,
        RAT, "NVDA",
        f"calculate_ratio('NVDA','debt_to_equity','annual') = "
        f"{ratio[('NVDA','debt_to_equity')]['formatted']}",
        "Not in the ingested filings -- the balance sheet is not indexed, so "
        "this is only reachable through the tool.")

    add("aapl-debt-to-equity-tool",
        "What is Apple's debt-to-equity ratio?",
        round(ratio[("AAPL", "debt_to_equity")]["value"], 2), X, 0.05,
        RAT, "AAPL",
        f"calculate_ratio('AAPL','debt_to_equity','annual') = "
        f"{ratio[('AAPL','debt_to_equity')]['formatted']}",
        "Balance-sheet only; not answerable from the indexed filings.")

    add("nvda-current-ratio-tool",
        "What is NVIDIA's current ratio?",
        round(ratio[("NVDA", "current_ratio")]["value"], 2), X, 0.05,
        RAT, "NVDA",
        f"calculate_ratio('NVDA','current_ratio','annual') = "
        f"{ratio[('NVDA','current_ratio')]['formatted']}",
        "Balance-sheet only.")

    add("aapl-net-margin-tool",
        "What is Apple's most recent annual net profit margin?",
        round(ratio[("AAPL", "net_margin")]["value"] * 100, 2), PCT, 0.5,
        RAT, "AAPL",
        f"calculate_ratio('AAPL','net_margin','annual') = "
        f"{ratio[('AAPL','net_margin')]['formatted']}")

    # ── tools: prices (closed historical windows) ───────────────────────────
    nvda_aug, aapl_aug = prices["nvda_aug"], prices["aapl_aug"]
    nvda_jul, aapl_jun = prices["nvda_jul"], prices["aapl_jun"]

    def price_tol(value: float) -> float:
        return round(max(1.0, abs(value) * 0.02), 2)

    add("nvda-close-2026-08-07",
        "What did NVIDIA stock close at on August 7, 2026?",
        nvda_aug["last"]["close"], USD, price_tol(nvda_aug["last"]["close"]),
        PRI, "NVDA",
        f"get_stock_price('NVDA','2026-08-03','2026-08-07').last = "
        f"{nvda_aug['last']['close']}")

    add("nvda-change-aug-week",
        "How much did NVIDIA stock move, in percent, between August 3 and "
        "August 7, 2026?",
        round(nvda_aug["change_pct"], 2), PCT, 1.0, PRI, "NVDA",
        f"get_stock_price('NVDA','2026-08-03','2026-08-07').change_pct = "
        f"{nvda_aug['change_pct']}")

    add("aapl-close-2026-08-07",
        "What did Apple stock close at on August 7, 2026?",
        aapl_aug["last"]["close"], USD, price_tol(aapl_aug["last"]["close"]),
        PRI, "AAPL",
        f"get_stock_price('AAPL','2026-08-03','2026-08-07').last = "
        f"{aapl_aug['last']['close']}")

    add("nvda-july-high",
        "What was NVIDIA's highest closing price during July 2026?",
        nvda_jul["high"]["close"], USD, price_tol(nvda_jul["high"]["close"]),
        PRI, "NVDA",
        f"get_stock_price('NVDA','2026-07-01','2026-07-31').high = "
        f"{nvda_jul['high']['close']} on {nvda_jul['high']['date']}")

    add("aapl-june-change",
        "How did Apple stock perform over June 2026, in percent?",
        round(aapl_jun["change_pct"], 2), PCT, 1.5, PRI, "AAPL",
        f"get_stock_price('AAPL','2026-06-01','2026-06-30').change_pct = "
        f"{aapl_jun['change_pct']}",
        "Negative: the month was down.")

    add("nvda-july-low",
        "What was NVIDIA's lowest closing price in July 2026?",
        nvda_jul["low"]["close"], USD, price_tol(nvda_jul["low"]["close"]),
        PRI, "NVDA",
        f"get_stock_price('NVDA','2026-07-01','2026-07-31').low = "
        f"{nvda_jul['low']['close']}")

    # ── genuinely needs both a filing figure and a live tool ────────────────
    nvda_close = nvda_aug["last"]["close"]
    aapl_close = aapl_aug["last"]["close"]
    nvda_dil_shares = nv("Diluted#2")
    aapl_dil_shares = ap("Diluted#2")

    nvda_cap = billions(nvda_dil_shares * nvda_close)
    aapl_cap = billions(aapl_dil_shares * aapl_close)

    add("nvda-implied-market-cap",
        "Using NVIDIA's fiscal 2026 diluted share count and its August 7, 2026 "
        "closing price, what was its implied market capitalisation?",
        nvda_cap, B, round(nvda_cap * 0.03, 1),
        ["search_filings", "get_stock_price"], "NVDA",
        f"Computed: {millions(nvda_dil_shares):,.0f}M diluted shares (filing) "
        f"x ${nvda_close} close (tool)",
        "The share count is only in the filing; the price is only in the tool.")

    add("aapl-implied-market-cap",
        "Using Apple's fiscal 2025 diluted share count and its August 7, 2026 "
        "closing price, what was its implied market capitalisation?",
        aapl_cap, B, round(aapl_cap * 0.03, 1),
        ["search_filings", "get_stock_price"], "AAPL",
        f"Computed: {millions(aapl_dil_shares):,.0f}M diluted shares (filing) "
        f"x ${aapl_close} close (tool)")

    nvda_pe = round(nvda_close / nv("Diluted"), 2)
    aapl_pe = round(aapl_close / ap("Diluted"), 2)

    add("nvda-implied-pe-on-fy2026-eps",
        "At its August 7, 2026 closing price, what P/E ratio does NVIDIA's "
        "fiscal 2026 diluted EPS imply?",
        nvda_pe, X, round(nvda_pe * 0.03, 2),
        ["search_filings", "get_stock_price"], "NVDA",
        f"Computed: ${nvda_close} close / ${nv('Diluted')} FY2026 diluted EPS",
        "Deliberately not calculate_ratio's pe_ratio, which uses trailing "
        "twelve-month EPS and today's price.")

    add("aapl-implied-pe-on-fy2025-eps",
        "At its August 7, 2026 closing price, what P/E ratio does Apple's "
        "fiscal 2025 diluted EPS imply?",
        aapl_pe, X, round(aapl_pe * 0.03, 2),
        ["search_filings", "get_stock_price"], "AAPL",
        f"Computed: ${aapl_close} close / ${ap('Diluted')} FY2025 diluted EPS")

    add("nvda-vs-aapl-gross-margin-gap",
        "How many percentage points higher was NVIDIA's most recent gross "
        "margin than Apple's?",
        round((ratio[("NVDA", "gross_margin")]["value"]
               - ratio[("AAPL", "gross_margin")]["value"]) * 100, 2),
        "percentage points", 1.0, RAT, "NVDA+AAPL",
        f"Computed: {ratio[('NVDA','gross_margin')]['formatted']} - "
        f"{ratio[('AAPL','gross_margin')]['formatted']}",
        "Two tool calls in one question; different fiscal year ends, which the "
        "answer should acknowledge.")

    return cases


def main() -> None:
    cases = build()
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tolerance_semantics": (
            "Absolute, in the case's `unit`. For unit 'percent' the tolerance is "
            "in percentage points, not a relative percentage."
        ),
        "corpus": {
            "NVDA": "10-K FY2026 (period ending 2026-01-25): Item 1A, Item 7, "
                    "consolidated statements of income",
            "AAPL": "10-K FY2025 (period ending 2025-09-27): Item 1A, Item 7, "
                    "consolidated statements of operations",
        },
        "cases": cases,
    }
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {OUT}  ({len(cases)} cases)")


main()
