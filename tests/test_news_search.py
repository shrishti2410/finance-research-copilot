"""Tests for the news tool. No live calls -- `requests.get` is replaced.

Feeds are built as real RSS 2.0 XML rather than stubbed at the parse boundary,
so the parsing is exercised: Google News' "Headline - Publisher" titles, HTML
inside <description>, and RFC 822 dates.
"""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from tools import news_search
from tools.base import ERROR_BAD_INPUT, ERROR_UPSTREAM


def rfc822(days_ago: float) -> str:
    return format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))


def rss(items: list[str], title: str = "Feed") -> bytes:
    body = "\n".join(items)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f"<rss version=\"2.0\"><channel><title>{title}</title>{body}</channel></rss>"
    ).encode()


def item(title: str, link: str, days_ago: float = 1, description: str = "Some text.",
         source: str | None = None, dated: bool = True) -> str:
    parts = [f"<title>{title}</title>", f"<link>{link}</link>",
             f"<description>{description}</description>"]
    if dated:
        parts.append(f"<pubDate>{rfc822(days_ago)}</pubDate>")
    if source:
        parts.append(f'<source url="https://example.com">{source}</source>')
    return "<item>" + "".join(parts) + "</item>"


GOOGLE_FEED = rss([
    item("NVIDIA beats expectations - Bloomberg", "https://ex.com/a", days_ago=1),
    item("Chip export rules tighten - Reuters", "https://ex.com/b", days_ago=3),
    item("Old news about NVIDIA - CNBC", "https://ex.com/c", days_ago=40),
])

# An item carrying an explicit <source> element rather than a title suffix.
YAHOO_FEED = rss([
    item("NVDA hits record high", "https://ex.com/d", days_ago=2, source="Yahoo Finance"),
])


@pytest.fixture
def http(monkeypatch):
    """Route every fetch through a recorded fake. Returns the knob and the log."""
    state: dict = {"responses": {}, "default": GOOGLE_FEED, "raise_for": set()}
    requested: list[str] = []

    class FakeResponse:
        def __init__(self, content, status=200):
            self.content = content
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code} Server Error")

    def fake_get(url, **kwargs):
        requested.append(url)
        for fragment in state["raise_for"]:
            if fragment in url:
                raise requests.ConnectionError("name resolution failed")
        for fragment, payload in state["responses"].items():
            if fragment in url:
                # An int stands for an HTTP status; bytes for a body.
                if isinstance(payload, int):
                    return FakeResponse(b"", payload)
                return FakeResponse(payload)
        return FakeResponse(state["default"])

    monkeypatch.setattr(news_search.requests, "get", fake_get)
    state["requested"] = requested
    return state


# ── parsing and normalization ────────────────────────────────────────────────

def test_returns_normalized_articles(http):
    result = news_search.search_news("NVIDIA", days_back=7)

    assert result["ok"] is True
    assert result["count"] == 2          # the 40-day-old item is outside the window
    article = result["articles"][0]
    assert set(article) == {"title", "source", "published", "url", "snippet"}
    assert article["url"].startswith("https://")


def test_publisher_is_split_out_of_a_google_news_title(http):
    """Google renders titles as 'Headline - Publisher'. Leaving the suffix on
    puts the publisher inside every headline the model reads."""
    titles = {a["title"]: a["source"] for a in news_search.search_news("NVIDIA")["articles"]}
    assert "NVIDIA beats expectations" in titles
    assert titles["NVIDIA beats expectations"] == "Bloomberg"


def test_the_suffix_is_stripped_even_when_source_is_also_supplied(http):
    """Google News sends both. Reading the element and leaving the suffix on --
    which a live call showed -- puts the publisher inside every headline."""
    http["default"] = rss([
        item("Nvidia Stock: Buy or Sell? - The Globe and Mail", "https://ex.com/g",
             source="The Globe and Mail")
    ])
    article = news_search.search_news("NVDA")["articles"][0]
    assert article["title"] == "Nvidia Stock: Buy or Sell?"
    assert article["source"] == "The Globe and Mail"


def test_a_title_suffix_that_is_not_the_publisher_is_left_alone(http):
    """Headlines contain " - " legitimately; only the real publisher comes off."""
    http["default"] = rss([
        item("Nvidia beats - and raises guidance", "https://ex.com/h", source="Reuters")
    ])
    article = news_search.search_news("NVDA")["articles"][0]
    assert article["title"] == "Nvidia beats - and raises guidance"


def test_a_snippet_that_only_repeats_the_title_is_dropped(http):
    """Google News descriptions are the headline again as a link. Keeping them
    doubles the tokens per article for no information."""
    http["default"] = rss([
        item("Nvidia beats expectations - Bloomberg", "https://ex.com/s",
             description='<a href="x">Nvidia beats expectations</a>&nbsp;&nbsp;Bloomberg')
    ])
    assert news_search.search_news("NVDA")["articles"][0]["snippet"] == ""


def test_html_entities_are_decoded(http):
    """Google escapes HTML inside the XML, so the feed carries "&amp;nbsp;" and
    lxml hands back the literal text "&nbsp;". Left alone it reaches the model
    verbatim -- the live NVDA call was full of them."""
    http["default"] = rss([
        item("Headline - Wire", "https://ex.com/e",
             description="Chips &amp;amp; margins under pressure&amp;nbsp;today.")
    ])
    assert news_search.search_news("NVDA")["articles"][0]["snippet"] == (
        "Chips & margins under pressure today."
    )


def test_explicit_source_element_wins_over_the_title_suffix(http):
    """Google News carries <source>; when it does, that beats guessing from the
    title, which would mangle a headline that legitimately contains " - "."""
    http["default"] = YAHOO_FEED
    assert news_search.search_news("NVDA")["articles"][0]["source"] == "Yahoo Finance"


def test_html_is_stripped_from_snippets(http):
    http["default"] = rss([
        item("Headline - Wire", "https://ex.com/x",
             description='<a href="https://ex.com">NVIDIA</a> said <b>revenue</b> rose.')
    ])
    assert news_search.search_news("NVIDIA")["articles"][0]["snippet"] == (
        "NVIDIA said revenue rose."
    )


def test_articles_are_newest_first(http):
    stamps = [a["published"] for a in news_search.search_news("NVIDIA", 30)["articles"]]
    assert stamps == sorted(stamps, reverse=True)


def test_published_is_iso_8601(http):
    published = news_search.search_news("NVIDIA")["articles"][0]["published"]
    datetime.fromisoformat(published)  # raises if it is not


# ── the date window ──────────────────────────────────────────────────────────

def test_articles_older_than_the_window_are_excluded(http):
    titles = [a["title"] for a in news_search.search_news("NVIDIA", days_back=7)["articles"]]
    assert "Old news about NVIDIA" not in titles


def test_a_wider_window_includes_them(http):
    titles = [a["title"] for a in news_search.search_news("NVIDIA", days_back=60)["articles"]]
    assert "Old news about NVIDIA" in titles


def test_undated_articles_are_kept(http):
    """Feeds omit pubDate often enough that dropping them loses real headlines."""
    http["default"] = rss([item("No date here - Wire", "https://ex.com/n", dated=False)])
    result = news_search.search_news("NVIDIA")

    assert result["count"] == 1
    assert result["articles"][0]["published"] is None


# ── source selection ─────────────────────────────────────────────────────────

def test_only_google_news_is_queried(http):
    """Yahoo's per-ticker feed was removed after a live call for NVDA returned
    Procter & Gamble and Deere. See the module docstring."""
    news_search.search_news("NVDA")
    assert len(http["requested"]) == 1
    assert "news.google.com" in http["requested"][0]
    assert not any("yahoo" in url for url in http["requested"])


def test_the_recency_operator_is_passed_to_google(http):
    news_search.search_news("NVIDIA", days_back=14)
    assert "when%3A14d" in http["requested"][0]


def test_the_query_is_url_encoded(http):
    news_search.search_news("AT&T earnings")
    assert "AT%26T" in http["requested"][0]


def test_duplicate_headlines_are_collapsed(http):
    """Aggregated feeds carry the same story from several publishers. Showing it
    twice wastes the model's context and inflates the apparent volume."""
    http["default"] = rss([
        item("NVIDIA beats expectations - Bloomberg", "https://ex.com/a"),
        item("NVIDIA beats expectations - Reuters", "https://other.com/a"),
    ])
    titles = [a["title"] for a in news_search.search_news("NVDA")["articles"]]
    assert titles.count("NVIDIA beats expectations") == 1


# ── failure handling ─────────────────────────────────────────────────────────

def test_an_unreachable_feed_is_an_upstream_error(http):
    """Distinct from 'no news': one means retry, the other means answer."""
    http["raise_for"] = {"news.google.com"}
    result = news_search.search_news("NVDA")

    assert result["ok"] is False
    assert result["error"] == ERROR_UPSTREAM
    assert "not a bad query" in result["message"]


def test_an_http_error_status_is_a_feed_failure(http):
    """A 503 body parses to zero items, which would otherwise look like 'no
    news' instead of 'the publisher is down'."""
    http["responses"] = {"news.google.com": 503}
    result = news_search.search_news("NVDA")

    assert result["ok"] is False
    assert result["error"] == ERROR_UPSTREAM
    assert "HTTPError" in result["message"]


def test_malformed_xml_yields_no_articles_rather_than_raising(http):
    http["default"] = b"<rss><channel><item><title>unclosed"
    result = news_search.search_news("NVIDIA")
    assert result["ok"] is True


def test_items_without_a_link_are_skipped(http):
    http["default"] = rss(["<item><title>Headline only</title></item>"])
    assert news_search.search_news("NVIDIA")["count"] == 0


# ── empty results are an answer, not a failure ───────────────────────────────

def test_no_results_is_ok_with_a_coverage_note(http):
    http["default"] = rss([])
    result = news_search.search_news("NVIDIA")

    assert result["ok"] is True
    assert result["count"] == 0
    assert "not evidence that no news exists" in result["coverage_note"]


def test_short_coverage_is_flagged(http):
    """A model must not read 'nothing older than 2 days' as 'nothing happened
    in the last 30'."""
    http["default"] = rss([item("Recent - Wire", "https://ex.com/r", days_ago=1)])
    result = news_search.search_news("NVIDIA", days_back=30)
    assert "rolled out of it" in result["coverage_note"]


def test_hitting_the_cap_is_not_reported_as_short_coverage(http):
    """A busy feed returns 25 items all from today. That is the article cap
    biting, not the feeds failing to reach back -- saying otherwise tells the
    model there is no older news when it simply was not shown."""
    http["default"] = rss([
        item(f"Story {i} - Wire", f"https://ex.com/{i}", days_ago=0.1)
        for i in range(40)
    ])
    result = news_search.search_news("NVIDIA", days_back=30)

    assert result["count"] == news_search.MAX_ARTICLES
    assert "Capped at" in result["coverage_note"]
    assert "rolled out" not in result["coverage_note"]


# ── bad input ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_query_is_rejected(http, bad):
    assert news_search.search_news(bad)["error"] == ERROR_BAD_INPUT


@pytest.mark.parametrize("bad", [0, -1, 91, 1000])
def test_days_back_out_of_range_is_rejected(http, bad):
    result = news_search.search_news("NVDA", days_back=bad)
    assert result["error"] == ERROR_BAD_INPUT
    assert "between 1 and 90" in result["message"]


def test_non_numeric_days_back_is_rejected(http):
    assert news_search.search_news("NVDA", days_back="a week")["error"] == ERROR_BAD_INPUT


def test_bad_input_never_reaches_the_network(http):
    news_search.search_news("NVDA", days_back=999)
    assert http["requested"] == []


def test_the_user_agent_carries_no_personal_details(http):
    assert "@" not in news_search.USER_AGENT
