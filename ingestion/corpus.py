"""The step-4 corpus: 15 companies, their last three annual 10-Ks, pinned.

Separate from `PINNED_FILINGS` in ingestion/pipeline.py, which is the eval's
corpus (one filing each for NVDA and AAPL) and must not move with this one.
The questions in eval/questions.json were written against that one.

Pinned by accession number for the reason the eval filings are: "the last
three" changes the day a company files again, and every measurement taken on
the old set would then describe a different corpus. The accession numbers were
resolved on 2026-11-16 with `python scripts/pin_corpus.py`; run it again to
move them deliberately.

Facts are pinned by date for the same reason: SEC's company-facts document
grows with each report, and a later report can restate an earlier year.
"""

from __future__ import annotations

from datetime import date

CORPUS_TICKERS: tuple[str, ...] = (
    "AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "JPM",
    "V", "JNJ", "WMT", "XOM", "PG", "KO", "NFLX",
)

# Filled in from scripts/pin_corpus.py; see the module docstring.
CORPUS_FILINGS: dict[str, tuple[str, ...]] = {
    "AAPL": ("0000320193-26-000026", "0000320193-25-000079", "0000320193-24-000123",),
    # Apple Inc.: periods 2026-09-26 filed 2026-11-03, 2025-09-27 filed 2025-10-31, 2024-09-28 filed 2024-11-01
    "NVDA": ("0001045810-26-000021", "0001045810-25-000023", "0001045810-24-000029",),
    # NVIDIA CORP: periods 2026-01-25 filed 2026-02-25, 2025-01-26 filed 2025-02-26, 2024-01-28 filed 2024-02-21
    "MSFT": ("0001193125-26-323660", "0000950170-25-100235", "0000950170-24-087843",),
    # MICROSOFT CORP: periods 2026-06-30 filed 2026-07-29, 2025-06-30 filed 2025-07-30, 2024-06-30 filed 2024-07-30
    "GOOGL": ("0001652044-26-000018", "0001652044-25-000014", "0001652044-24-000022",),
    # Alphabet Inc.: periods 2025-12-31 filed 2026-02-05, 2024-12-31 filed 2025-02-05, 2023-12-31 filed 2024-01-31
    "AMZN": ("0001018724-26-000004", "0001018724-25-000004", "0001018724-24-000008",),
    # AMAZON COM INC: periods 2025-12-31 filed 2026-02-06, 2024-12-31 filed 2025-02-07, 2023-12-31 filed 2024-02-02
    "META": ("0001628280-26-003942", "0001326801-25-000017", "0001326801-24-000012",),
    # Meta Platforms, Inc.: periods 2025-12-31 filed 2026-01-29, 2024-12-31 filed 2025-01-30, 2023-12-31 filed 2024-02-02
    "TSLA": ("0001628280-26-003952", "0001628280-25-003063", "0001628280-24-002390",),
    # Tesla, Inc.: periods 2025-12-31 filed 2026-01-29, 2024-12-31 filed 2025-01-30, 2023-12-31 filed 2024-01-29
    "JPM": ("0001628280-26-008131", "0000019617-25-000270", "0000019617-24-000225",),
    # JPMORGAN CHASE & CO: periods 2025-12-31 filed 2026-02-13, 2024-12-31 filed 2025-02-14, 2023-12-31 filed 2024-02-16
    "V": ("0001403161-26-000134", "0001403161-25-000089", "0001403161-24-000058",),
    # VISA INC.: periods 2026-09-30 filed 2026-11-05, 2025-09-30 filed 2025-11-06, 2024-09-30 filed 2024-11-13
    "JNJ": ("0000200406-26-000016", "0000200406-25-000038", "0000200406-24-000013",),
    # JOHNSON & JOHNSON: periods 2025-12-28 filed 2026-02-11, 2024-12-29 filed 2025-02-13, 2023-12-31 filed 2024-02-16
    "WMT": ("0000104169-26-000055", "0000104169-25-000021", "0000104169-24-000056",),
    # Walmart Inc.: periods 2026-01-31 filed 2026-03-13, 2025-01-31 filed 2025-03-14, 2024-01-31 filed 2024-03-15
    "XOM": ("0000034088-26-000045", "0000034088-25-000010", "0000034088-24-000018",),
    # EXXON MOBIL CORP: periods 2025-12-31 filed 2026-02-18, 2024-12-31 filed 2025-02-19, 2023-12-31 filed 2024-02-28
    "PG": ("0000080424-26-000103", "0000080424-25-000076", "0000080424-24-000083",),
    # PROCTER & GAMBLE Co: periods 2026-06-30 filed 2026-08-04, 2025-06-30 filed 2025-08-04, 2024-06-30 filed 2024-08-05
    "KO": ("0001628280-26-010047", "0000021344-25-000011", "0000021344-24-000009",),
    # COCA COLA CO: periods 2025-12-31 filed 2026-02-20, 2024-12-31 filed 2025-02-20, 2023-12-31 filed 2024-02-20
    "NFLX": ("0001065280-26-000034", "0001065280-25-000044", "0001065280-24-000030",),
    # NETFLIX INC: periods 2025-12-31 filed 2026-01-23, 2024-12-31 filed 2025-01-27, 2023-12-31 filed 2024-01-26
}

CORPUS_FACTS_THROUGH = date(2026, 11, 16)

# Tickers SEC's ticker map no longer sends to the filer wanted here. Checked on
# 2026-11-16: "XOM" maps to ExxonMobil Holdings Corp (CIK 2115436, first filing
# 2026-07-01, no 10-K yet); Exxon Mobil Corp's 10-Ks, through the one filed
# 2026-02-18, are under CIK 34088.
CORPUS_CIK_OVERRIDES: dict[str, str] = {"XOM": "0000034088"}
