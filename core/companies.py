"""The companies the site covers, and one resolver from free text to a company.

The 5 NSE watchlist stocks and the 15 US companies whose 10-Ks are indexed
(the 3 US watchlist stocks are among them): 20 companies. Used by the news
tool today; search and every agent tool are to use the same resolver (CLAUDE.md,
P0.1).

Names are the exchanges' and SEC's own, checked on 2026-11-18:
- NSE: the listed-equity file (EQUITY_L.csv): name, symbol and ISIN. The file
  itself is not committed (decision of 2026-11-18, docs/P0_PROPOSAL.md).
- US: the company names SEC returns for the pinned 10-Ks (ingestion/corpus.py).
BSE scrip codes are listed only where verified: Dixon's 540699 was supplied by
the user; the others are left out rather than guessed.

Aliases come in two strengths, because news search is free text:
- strong: names nothing else ("Dixon Technologies", "Nvidia", "HDFC Bank");
  a headline that has one is about the company.
- weak: a common word or a name others share ("Apple", "Visa", "Meta", "SBI");
  a headline counts only if it also has market context (shares, stock, NSE,
  earnings, crore...). "Dixon" alone is not even weak: in a 2026-11-18 search,
  every one of the first 40 "Dixon" headlines was about someone else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Company:
    symbol: str                    # as the tools take it: "DIXON.NS", "NVDA"
    name: str                      # the exchange's or SEC's name
    exchange: str                  # "NSE", "NASDAQ", "NYSE"
    strong: tuple[str, ...]        # unambiguous names
    weak: tuple[str, ...] = ()     # need market context in the same headline
    ids: dict[str, str] = field(default_factory=dict)   # NSE / BSE / ISIN codes
    # Short names a person types that resolve to the company in a query but are
    # too ambiguous to identify it in a headline ("Dixon").
    short: tuple[str, ...] = ()

    @property
    def ticker(self) -> str:
        return self.symbol.split(".")[0]


COVERED: tuple[Company, ...] = (
    # NSE watchlist (names, symbols and ISINs from NSE's EQUITY_L.csv, 2026-11-18)
    Company("COFORGE.NS", "Coforge Limited", "NSE", ("Coforge",),
            ids={"NSE": "COFORGE", "ISIN": "INE591G01025"}),
    Company("DIXON.NS", "Dixon Technologies (India) Limited", "NSE",
            ("Dixon Technologies", "Dixon Tech"),
            ids={"NSE": "DIXON", "BSE": "540699", "ISIN": "INE935N01020"}, short=("Dixon",)),
    Company("SBIN.NS", "State Bank of India", "NSE", ("State Bank of India",), ("SBI",),
            ids={"NSE": "SBIN", "ISIN": "INE062A01020"}),
    Company("PREMIERENE.NS", "Premier Energies Limited", "NSE", ("Premier Energies",),
            ids={"NSE": "PREMIERENE", "ISIN": "INE0BS701011"}),
    Company("HDFCBANK.NS", "HDFC Bank Limited", "NSE", ("HDFC Bank",),
            ids={"NSE": "HDFCBANK", "ISIN": "INE040A01034"}),
    # The 15 US companies with indexed 10-Ks (names as SEC gives them)
    Company("AAPL", "Apple Inc.", "NASDAQ", ("Apple Inc",), ("Apple",)),
    Company("NVDA", "NVIDIA Corporation", "NASDAQ", ("Nvidia",)),
    Company("MSFT", "Microsoft Corporation", "NASDAQ", ("Microsoft",)),
    Company("GOOGL", "Alphabet Inc.", "NASDAQ", ("Alphabet Inc", "Google"), ("Alphabet",)),
    Company("AMZN", "Amazon.com, Inc.", "NASDAQ", ("Amazon.com", "Amazon Web Services"), ("Amazon", "AWS")),
    Company("META", "Meta Platforms, Inc.", "NASDAQ", ("Meta Platforms",), ("Meta", "Facebook")),
    Company("TSLA", "Tesla, Inc.", "NASDAQ", ("Tesla",)),
    Company("JPM", "JPMorgan Chase & Co.", "NYSE", ("JPMorgan", "JP Morgan", "J.P. Morgan")),
    Company("V", "Visa Inc.", "NYSE", ("Visa Inc",), ("Visa",)),
    Company("JNJ", "Johnson & Johnson", "NYSE", ("Johnson & Johnson", "J&J")),
    Company("WMT", "Walmart Inc.", "NYSE", ("Walmart",)),
    Company("XOM", "Exxon Mobil Corporation", "NYSE", ("Exxon", "ExxonMobil")),
    Company("PG", "The Procter & Gamble Company", "NYSE", ("Procter & Gamble", "P&G")),
    Company("KO", "The Coca-Cola Company", "NYSE", ("Coca-Cola", "Coca Cola")),
    Company("NFLX", "Netflix, Inc.", "NASDAQ", ("Netflix",)),
)

# Words that make a weak alias count: the headline is about the market.
MARKET_CONTEXT = re.compile(
    r"\b(shares?|stocks?|nasdaq|nyse|nse|bse|sensex|nifty|earnings|revenue|profit|"
    r"crore|lakh|quarterly|market cap|analysts?|inc\.?|ltd\.?|limited|corp\.?|"
    r"dividend|buyback|investors?|valuation|price target|q[1-4]|fy\d{2,4})\b", re.I)

# Short tickers ("V", "KO", "PG") are words; they count only exchange-qualified.
_MIN_BARE_TICKER = 3


def _has(phrase: str, text: str) -> bool:
    return re.search(rf"(?<![\w&]){re.escape(phrase)}(?![\w&])", text, re.I) is not None


def _ticker_pattern(c: Company) -> re.Pattern:
    """The ticker as an exchange-qualified code ("NSE: DIXON", "NASDAQ:NVDA"),
    a Yahoo symbol ("DIXON.NS"), a code (BSE 540699, ISIN), or, for a ticker of
    3+ letters, the bare upper-case ticker ("NVDA")."""
    t = re.escape(c.ticker)
    alts = [rf"\b(?:NSE|BSE|NASDAQ|NYSE)\s*:\s*{t}\b", rf"\b{re.escape(c.symbol)}\b"]
    alts += [rf"\b{re.escape(v)}\b" for k, v in c.ids.items() if k in ("BSE", "ISIN")]
    if len(c.ticker) >= _MIN_BARE_TICKER:
        alts.append(rf"(?<![\w$]){t}(?![\w])")
    return re.compile("|".join(alts))          # case-sensitive: "NVDA", not "nvda"


def names_company(c: Company, text: str) -> bool:
    """Whether `text` (a headline and its snippet) is about company `c`."""
    if any(_has(a, text) for a in (c.name, *c.strong)) or _ticker_pattern(c).search(text):
        return True
    return any(_has(a, text) for a in c.weak) and MARKET_CONTEXT.search(text) is not None


def resolve(text: str) -> Company | None:
    """The covered company `text` names, or None.

    Matches a symbol ("DIXON.NS"), an exchange code ("NSE:DIXON"), the
    company's name, an alias or a listed short name ("Dixon"), each as a whole
    word and case insensitive; a ticker of up to 3 letters ("KO", "PG") only in
    capitals. The longest match wins, so "Dixon Technologies" beats "Dixon".
    """
    q = (text or "").strip()
    if not q:
        return None
    best: tuple[int, Company] | None = None
    for c in COVERED:
        candidates = [c.symbol, c.name, *c.strong, *c.weak, *c.short,
                      *(f"{k}:{v}" for k, v in c.ids.items()),
                      c.ticker if len(c.ticker) >= 4 else None]
        hits = [cand for cand in filter(None, candidates) if cand.upper() == q.upper() or _has(cand, q)]
        if 2 <= len(c.ticker) <= 3 and re.search(rf"(?<![\w$]){re.escape(c.ticker)}(?!\w)", q):
            hits.append(c.ticker)
        for cand in hits:
            if best is None or len(cand) > best[0]:
                best = (len(cand), c)
    return best[1] if best else None


def search_terms(c: Company) -> list[str]:
    """What to put in a news query for `c`: its unambiguous names and its
    exchange identifiers, quoted where they have spaces."""
    terms = [f'"{a}"' if " " in a else a for a in c.strong]
    if c.exchange == "NSE":
        terms.append(f"NSE:{c.ids['NSE']}")
    else:
        terms.append(f"{c.exchange}:{c.ticker}")
    if "BSE" in c.ids:
        terms.append(c.ids["BSE"])
    return terms
