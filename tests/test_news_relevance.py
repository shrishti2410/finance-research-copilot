"""News is about the company asked for, never about everyone who shares its
name (live-site feedback, 2026-11-18: "Dixon" returned the City of Dixon, a
MotoGP rider and people named Dixon).

The headlines below are real: Google News results for "Dixon", '"City of
Dixon"' and "Dixon MotoGP" fetched on 2026-11-18. The four Vimal Plast items
are the real coverage of Dixon Technologies' asset purchase (NSE filing,
16 Nov 2026). No live calls: requests.get is replaced, as in
test_news_search.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import unquote_plus

import pytest
import requests

from core.companies import COVERED, resolve
from tools import news_search


def _item(title, source, days_ago=1.0, description=""):
    when = format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))
    return (f"<item><title>{title} - {source}</title><link>https://ex.com/{abs(hash(title))}</link>"
            f"<description>{description}</description><pubDate>{when}</pubDate>"
            f'<source url="https://ex.com">{source}</source></item>')


def _rss(items):
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>F</title>'
            + "".join(items) + "</channel></rss>").encode()


# Real headlines that name "Dixon" and are not about Dixon Technologies.
IRRELEVANT = [
    ("Mike Dixon Obituary Oct 20, 2026", "Shackelford Funeral Directors"),
    ("Collin Dixon Named One of 13 Biletnikoff Semifinalists", "University of Illinois Athletics"),
    ("Rapids Academy midfielder Luke Dixon called up to U.S. U-15 Boys’ National Team", "Colorado Rapids"),
    ("Joey Dixon's five strikeouts over two innings", "MLB.com"),
    ("City of Dixon Launches Shop Local Campaign to Support Local Businesses", "Independent Voice"),
    ("Dixon announces upcoming hydrant flushing schedule", "Shaw Local"),
    ("Official: Jake Dixon gets \"dream\" Honda MotoGP chance in Qatar", "Crash.net"),
    ("Jake Dixon Replaces Joan Mir at Honda for MotoGP Qatar. Here’s What It Fixes, and What It Doesn’t",
     "Yahoo Sports"),
    ("Dixon replaces Mir at the Portuguese GP", "motogp.com"),
    ("Sherry Rozell and Valerie Dixon earn 2026 Missouri &amp; Kansas Super Lawyers honors", "McAfee &amp; Taft"),
    # Unrelated items the targeted search also returned.
    ("Wendt India Limited Reports Financial Results for Q2 and H1 FY27", "EquityBulls"),
    ("NHPC commissions Unit 5 of Subansiri Lower Hydro Electric Project", "EquityBulls"),
]

# The real coverage of the Vimal Plast asset purchase.
VIMAL_PLAST = [
    ("Dixon Technologies (India) Enters Asset Purchase Agreement With Vimal Plast", "TradingView", 2.0),
    ("Dixon Technologies to Acquire Vimal Plast Assets", "www.marketscreener.com", 1.0),
    ("Dixon Tech shares rise 3% as company buys new assets to boost washing machine operations",
     "Moneycontrol.com", 1.0),
    ("Dixon Technologies (NSE: DIXON): Asset Purchase Agreement Covers Manufacturing and Research Equipment",
     "Kalkine India", 1.0),
]


@pytest.fixture
def feed(monkeypatch):
    state = {"items": [], "requested": []}

    class Response:
        status_code = 200

        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        state["requested"].append(url)
        return Response(_rss(state["items"]))

    monkeypatch.setattr(news_search.requests, "get", fake_get)
    return state


# --- the resolver ---------------------------------------------------------------------

@pytest.mark.parametrize("text", ["Dixon", "dixon", "DIXON.NS", "NSE:DIXON", "Dixon Technologies",
                                  "Dixon Technologies (India) Limited", "Dixon Tech news"])
def test_dixon_resolves_to_dixon_technologies(text):
    company = resolve(text)
    assert company is not None and company.symbol == "DIXON.NS"


def test_every_covered_company_resolves_by_symbol_and_name():
    assert len(COVERED) == 20
    for c in COVERED:
        assert resolve(c.symbol) is c and resolve(c.name) is c


@pytest.mark.parametrize("text", ["semiconductor export controls", "interest rates", "Reliance Industries"])
def test_a_topic_or_an_uncovered_company_does_not_resolve(text):
    assert resolve(text) is None


# --- the query --------------------------------------------------------------------

def test_dixon_is_searched_by_full_name_and_exchange_identifiers(feed):
    news_search.search_news("Dixon")
    q = unquote_plus(feed["requested"][0])
    assert '"Dixon Technologies"' in q and "NSE:DIXON" in q and "540699" in q


# --- the filter ---------------------------------------------------------------------

def test_people_places_and_motorsport_named_dixon_are_dropped(feed):
    feed["items"] = [_item(t, s) for t, s in IRRELEVANT]
    result = news_search.search_news("Dixon", days_back=30)
    assert result["count"] == 0
    assert result["filtered_out"] == len(IRRELEVANT)


def test_the_vimal_plast_coverage_is_kept_and_ranked_first(feed):
    feed["items"] = ([_item(t, s, 0.5) for t, s in IRRELEVANT]
                     + [_item(t, s, d) for t, s, d in VIMAL_PLAST])
    result = news_search.search_news("Dixon", days_back=30)
    titles = [a["title"] for a in result["articles"]]
    assert sorted(titles) == sorted(t for t, _, _ in VIMAL_PLAST)   # nothing else survives
    assert all(a["published"] and a["source"] for a in result["articles"])   # date and source shown
    assert result["company"]["name"] == "Dixon Technologies (India) Limited"


def test_financial_coverage_ranks_above_newer_general_coverage(feed):
    feed["items"] = [
        _item("Dixon Technologies opens a new plant", "Some Local Blog", 0.1),
        _item("Dixon Technologies Q2 results: profit rises", "Economic Times", 2.0),
    ]
    titles = [a["title"] for a in news_search.search_news("Dixon", days_back=30)["articles"]]
    assert titles[0] == "Dixon Technologies Q2 results: profit rises"


def test_duplicates_across_publishers_are_collapsed(feed):
    feed["items"] = [_item("Dixon Technologies to Acquire Vimal Plast Assets", "MarketScreener"),
                     _item("Dixon Technologies to Acquire Vimal Plast Assets", "Moneycontrol.com")]
    assert news_search.search_news("Dixon", days_back=30)["count"] == 1


@pytest.mark.parametrize("title, keep", [
    ("Apple shares fall after iPhone sales miss", True),
    ("Apple Inc. raises its dividend", True),
    ("Apple picking season opens at local orchards", False),
    ("Best apple pie recipes for the holidays", False),
])
def test_an_ambiguous_name_needs_market_context(feed, title, keep):
    feed["items"] = [_item(title, "Some Publisher")]
    assert (news_search.search_news("Apple", days_back=30)["count"] == 1) is keep


def test_a_topic_query_is_filtered_by_its_own_words(feed):
    feed["items"] = [_item("New semiconductor export controls announced", "Reuters"),
                     _item("Local bakery wins award", "Patch")]
    result = news_search.search_news("semiconductor export controls", days_back=30)
    assert [a["title"] for a in result["articles"]] == ["New semiconductor export controls announced"]
    assert result["filtered_out"] == 1
