"""Recent news headlines, from public RSS feeds.

Why RSS and not NewsAPI
-----------------------
NewsAPI's free tier delays articles by 24 hours, caps at 100 requests/day, and
is licensed for development only. A `days_back` parameter over a feed that
cannot see today is a tool that lies about recency, so it is not usable here.
RSS needs no key, no quota and no account, which also means one less thing that
breaks for whoever clones this repo next.

Why Google News alone, and not Reuters or Yahoo
-----------------------------------------------
Reuters retired their public feeds (`feeds.reuters.com`) in 2020.

Yahoo Finance's per-ticker feed, `headline?s=NVDA`, was implemented here and
then removed after testing it live. It is an *association*, not a search: the
NVDA call came back with Procter & Gamble, Gilead and Deere headlines. Gating
those on whether they mention the query does not rescue it either, because a
ticker query is exactly the case where the headline says "Nvidia" and never
"NVDA" -- the filter drops the relevant articles along with the noise.

Google News RSS runs a real search. `NVDA when:7d` returns 100 items, all about
NVIDIA, and it aggregates Yahoo Finance among its publishers anyway -- so
Yahoo's content is still reachable here, just relevance-ranked first. One
well-targeted feed beats two where one is untargeted, because a model reading an
off-topic headline will summarize it as news about the company it asked for.

What this is not
----------------
A headline scanner, not an archive. The feed carries what publishers are
currently syndicating, with no server-side date filtering -- the window is
applied here, after fetching. Asking for 90 days back does not reach 90 days of
history; it filters whatever the feed currently holds. `coverage_note` says so
when it matters, so a model does not read an empty result as "nothing happened".
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus

import requests
from lxml import etree

from core.config import settings
from core.retry import RetryPolicy, retrying, status_is_transient
from tools.base import ERROR_BAD_INPUT, ERROR_UPSTREAM, error, ok

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)

# Descriptive, and deliberately carries no personal contact details.
USER_AGENT = "finance-research-copilot/0.1 (+https://github.com/; RSS reader)"

# (connect, read). Separately, because a feed that accepts the socket and then
# stalls is the common failure and one number cannot say "fail fast on
# unreachable, be patient once connected". Both come from core.config.
REQUEST_TIMEOUT = (settings.news_connect_timeout, settings.news_read_timeout)
MAX_ARTICLES = 25
MAX_DAYS_BACK = 90

# Google News renders titles as "Headline - Publisher"; the suffix is the source.
_TITLE_SOURCE = re.compile(r"\s+-\s+([^-]{2,40})$")


def _feeds(query: str, days: int) -> list[tuple[str, str]]:
    """(name, url) for every feed to query.

    A list of one today. It stays a list because the plumbing below -- partial
    failure, cross-feed dedupe, `sources_queried` -- is what makes adding a
    second source a one-line change, and it is already written.
    """
    # `when:Nd` is Google News' own recency operator. It does not replace the
    # local date filter -- Google honours it loosely -- but it biases the result
    # set toward the window asked for.
    return [
        ("Google News", GOOGLE_NEWS_RSS.format(query=quote_plus(f"{query} when:{days}d")))
    ]


def _transient(exc: BaseException) -> bool:
    """Whether this failure is worth trying again.

    Connection and timeout errors always are. An HTTP status is only sometimes:
    429 and 5xx are the publisher asking for patience, while a 404 means the
    feed is not there and asking twice more just spends the user's time.
    """
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return status_is_transient(exc.response.status_code)
    return isinstance(exc, requests.RequestException)


def _fetch_once(url: str) -> bytes:
    response = requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response.content


def _fetch(url: str) -> bytes:
    """Fetch a feed, retrying transient failures with backoff.

    Google News rate-limits bursts, and the agent can issue two news searches in
    one iteration, so a 429 here is a blip rather than an outage. The jitter in
    the backoff is what keeps those two parallel calls from colliding again on
    the retry.
    """
    policy = RetryPolicy(attempts=settings.news_retries, predicate=_transient)
    return retrying(lambda: _fetch_once(url), policy=policy,
                    description=f"news feed {url[:60]}")


def _text_of(element) -> str:
    """Element text with any embedded markup flattened.

    Google News descriptions are HTML fragments inside the XML, so taking
    `.text` alone truncates at the first tag.
    """
    if element is None:
        return ""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _published(item) -> datetime | None:
    raw = _text_of(item.find("pubDate"))
    if not raw:
        return None
    try:
        stamp = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    # A feed may omit the offset; treating a naive stamp as UTC keeps the
    # comparison against the cutoff from raising.
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp


def _parse_feed(payload: bytes, feed_name: str) -> list[dict[str, Any]]:
    """RSS 2.0 items, normalized. A malformed feed yields nothing, not an error."""
    try:
        # recover=True because publishers ship feeds with stray raw ampersands.
        root = etree.fromstring(payload, etree.XMLParser(recover=True))
    except etree.XMLSyntaxError:
        return []
    if root is None:
        return []

    items = []
    for item in root.iter("item"):
        title = _text_of(item.find("title"))
        link = _text_of(item.find("link"))
        if not title or not link:
            continue

        # Google News supplies <source> *and* repeats it as a title suffix, so
        # the suffix has to come off either way -- reading the element is not
        # enough. Without this every headline the model sees ends in
        # " - Publisher", and the publisher is already its own field.
        source = _text_of(item.find("source"))
        match = _TITLE_SOURCE.search(title)
        if match:
            suffix = match.group(1).strip()
            if not source or _normalized(suffix) == _normalized(source):
                source = source or suffix
                title = title[: match.start()].strip()

        snippet = _strip_html(_text_of(item.find("description")))
        # Google News descriptions are the headline again, as a link. Keeping
        # that doubles the tokens per article and adds nothing.
        if _normalized(snippet).startswith(_normalized(title)):
            snippet = ""

        items.append({
            "title": title,
            "source": source or feed_name,
            "published": _published(item),
            "url": link,
            "snippet": snippet[:400],
        })
    return items


def _dedupe_key(article: dict) -> str:
    return re.sub(r"[^a-z0-9]+", "", article["title"].lower())[:80]


def search_news(query: str, days_back: int = 7) -> dict[str, Any]:
    """Search recent news headlines about a company, ticker or topic.

    Use this for questions about what has recently been reported or announced.
    Results are headlines with short snippets and links, not full article text,
    and come from public news RSS -- so this covers what publishers are
    currently syndicating, not a searchable archive. Do not use it to establish
    that something did *not* happen.

    Args:
        query: What to search for. A company name ("NVIDIA"), a ticker
            ("NVDA"), or a topic ("semiconductor export controls"). Tickers and
            company names both work; the search is run as free text.
        days_back: How many days of history to include, counting back from
            today. Between 1 and 90; defaults to 7. Note this filters the
            headlines the feed currently carries -- a large value does not
            retrieve older articles that have already rolled out of it.

    Returns:
        On success, a dict with "ok": True and:
            query, days_back: what was searched, normalized.
            from_date: ISO timestamp of the oldest article included.
            count: number of articles returned, at most 25.
            articles: list, newest first, each with "title", "source" (the
                publisher), "published" (ISO 8601 timestamp, or None if the
                feed omitted one), "url", and "snippet". "snippet" is often
                an empty string: Google News' description field just repeats the
                headline, and a repeat is dropped rather than passed on. Treat
                the title as the content and the url as where to read more.
            sources_queried: which feeds answered.
            coverage_note: present when the result is empty, when it was capped,
                or when the feed reached back less far than requested --
                explaining that this reflects feed coverage rather than an
                absence of news.

        A search that simply found nothing returns "ok": True with count 0 --
        that is an answer, not a failure. Failures are
        {"ok": False, "error": code, "message": ...} where code is "bad_input"
        (empty query, or days_back out of range) or "upstream_error" (no feed
        could be reached).
    """
    text = (query or "").strip()
    if not text:
        return error(ERROR_BAD_INPUT, "query must be a non-empty search term.")

    try:
        days = int(days_back)
    except (TypeError, ValueError):
        return error(
            ERROR_BAD_INPUT, f"days_back must be a whole number, got {days_back!r}."
        )
    if not 1 <= days <= MAX_DAYS_BACK:
        return error(
            ERROR_BAD_INPUT,
            f"days_back must be between 1 and {MAX_DAYS_BACK}, got {days}.",
        )

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    collected: list[dict[str, Any]] = []
    reached: list[str] = []
    failures: list[str] = []

    for name, url in _feeds(text, days):
        try:
            collected.extend(_parse_feed(_fetch(url), name))
            reached.append(name)
        except requests.RequestException as exc:
            failures.append(f"{name}: {type(exc).__name__}")

    if not reached:
        return error(
            ERROR_UPSTREAM,
            f"Could not reach any news feed ({'; '.join(failures)}). "
            f"This is a network or publisher problem, not a bad query.",
            query=text, days_back=days,
        )

    # Keep undated items: a feed omitting pubDate is common, and dropping them
    # would silently lose real headlines.
    in_window = [
        article for article in collected
        if article["published"] is None or article["published"] >= cutoff
    ]

    seen: set[str] = set()
    unique = []
    for article in sorted(
        in_window,
        key=lambda a: a["published"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    ):
        key = _dedupe_key(article)
        if key in seen:
            continue
        seen.add(key)
        unique.append(article)

    articles = unique[:MAX_ARTICLES]
    dated = [a["published"] for a in articles if a["published"]]

    result = ok(
        query=text,
        days_back=days,
        from_date=min(dated).isoformat() if dated else None,
        count=len(articles),
        articles=[
            {
                "title": a["title"],
                "source": a["source"],
                "published": a["published"].isoformat() if a["published"] else None,
                "url": a["url"],
                "snippet": a["snippet"],
            }
            for a in articles
        ],
        sources_queried=reached,
        **({"sources_failed": failures} if failures else {}),
    )

    if not articles:
        result["coverage_note"] = (
            f"No headlines for {text!r} in the last {days} days from "
            f"{', '.join(reached)}. News RSS carries only what publishers are "
            f"currently syndicating, so this means the feed has nothing -- it is "
            f"not evidence that no news exists."
        )
    elif len(articles) >= MAX_ARTICLES:
        result["coverage_note"] = (
            f"Capped at {MAX_ARTICLES} articles. There is more coverage inside the "
            f"{days}-day window than is shown; narrow the query to see the rest."
        )
    elif dated and (datetime.now(timezone.utc) - min(dated)).days < days - 1:
        # Only meaningful when the cap was *not* hit. Otherwise a short reach
        # just means the feed is busy, which is not a coverage problem.
        result["coverage_note"] = (
            f"The feed only reached back "
            f"{(datetime.now(timezone.utc) - min(dated)).days} days of the {days} "
            f"requested. Older articles have rolled out of it."
        )
    return result
