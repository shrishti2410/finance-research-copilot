"""The query router: a plain lookup goes straight to its tool.

A question that is only a news, price or reported-figure lookup about one
covered company does not need the model to choose a tool. On the live site
(2026-11-18) such questions spent several model round trips choosing one and
hit ReadTimeouts under Groq's 8,000-tokens-a-minute limit. So:

    route(question) -> (tool, arguments)   the loop runs that tool, then makes
                                           one model call, with no tools, to
                                           write the answer
    route(question) -> None                the agent, as before

The answer is written on the same tool-free final step the agent uses, so the
grounding check, the [Unverified] marks and the advice check all apply.

Deliberately narrow: exactly one covered company (core/companies.py) and an
unmistakable intent. Analysis, comparisons, performance, predictions, dated
prices and derived figures (growth, margins, ratios) all go to the agent.
Reported figures are routed only for the 15 US companies whose XBRL facts are
indexed.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from core.companies import mentioned

# Anything that needs judgement, several steps or the snapshot: the agent's.
_AGENT_ONLY = re.compile(
    r"\b(analy[sz]\w*|overview|how (is|has|have|are|was|were|did)|doing|perform\w*|compar\w*|"
    r"versus|vs\.?|outlook|should|recommend\w*|predict\w*|forecast\w*|expect\w*|target|"
    r"why|explain\w*|signal|backtest\w*|strateg\w*|option\w*|risk\w*)\b", re.I)
_PREDICTION = re.compile(r"\bwill\b.*\b(go|rise|fall|drop|climb|reach|hit|be)\b", re.I)

_NEWS = re.compile(r"\b(news|headlines?|announc\w*|press release)\b", re.I)
_PRICE = re.compile(r"\b(share price|stock price|price|trading at|quote|last close|closing price)\b", re.I)
_DATED = re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b|\b(on|as of|since|between)\b", re.I)

# Reported figures the fact tool holds, by the words a person uses.
_FACTS = (
    (re.compile(r"\bdiluted (eps|earnings per share)\b|\beps\b|\bearnings per share\b", re.I), "eps_diluted"),
    (re.compile(r"\bnet income\b|\bnet profit\b", re.I), "net_income"),
    (re.compile(r"\brevenues?\b|\bnet sales\b", re.I), "revenue"),
)
_DERIVED = re.compile(r"\b(growth|grew|margin|ratio|change|increase|decrease|per cent|percent|%|"
                      r"compared|than|between|difference)\b", re.I)
_FISCAL_YEAR = re.compile(r"\b(?:fiscal(?: year)?\s*|fy\s*)(20\d{2}|\d{2})\b", re.I)
_ANY_YEAR = re.compile(r"\b(19|20)\d{2}\b")

NEWS_DAYS = 7
PRICE_LOOKBACK_DAYS = 14


def route(question: str, today: date | None = None) -> tuple[str, dict[str, Any]] | None:
    """(tool name, arguments) for a plain lookup about one covered company,
    else None for the agent."""
    q = (question or "").strip()
    companies = mentioned(q)
    if len(companies) != 1 or _AGENT_ONLY.search(q) or _PREDICTION.search(q):
        return None
    c = companies[0]

    if _NEWS.search(q):
        return "search_news", {"query": c.name, "days_back": NEWS_DAYS}

    if _PRICE.search(q):
        if _DATED.search(q) or _ANY_YEAR.search(q):
            return None
        start = (today or date.today()) - timedelta(days=PRICE_LOOKBACK_DAYS)
        return "get_stock_price", {"ticker": c.symbol, "start_date": start.isoformat()}

    if c.exchange != "NSE" and not _DERIVED.search(q):
        for pattern, fact in _FACTS:
            if pattern.search(q):
                fy = _FISCAL_YEAR.search(q)
                if fy:
                    year = int(fy.group(1))
                    return "get_financial_fact", {"ticker": c.symbol, "fact": fact,
                                                  "fiscal_year": year + 2000 if year < 100 else year}
                if _ANY_YEAR.search(q):
                    return None          # a calendar year is ambiguous: the agent asks or resolves
                return "get_financial_fact", {"ticker": c.symbol, "fact": fact, "fiscal_year": "latest"}
    return None
