"""Timeouts, retries, and what the tools do when an upstream misbehaves.

Every external call this project makes has to answer two questions: how long
will it wait, and what does the caller get when it fails. A tool that raises
instead of returning an error envelope does not crash anything -- the registry
catches it -- but the model then receives "get_stock_price raised KeyError:
'Open'" and writes that into an answer about a share price. That was a real
finding, not a hypothetical: see the two tests below.

Inference is deliberately absent from the retry tests. A stuck model call needs
a timeout and a failure, not a retry that doubles a 300s wait -- see
core/retry.py.
"""

import httpx
import pandas as pd
import pytest
import requests
import yfinance as yf

from core.config import settings
from core.retry import RetryPolicy, retrying, status_is_transient
from tools import news_search, ratios, stock_price


# ─────────────────────────────────────────────────────────────────────────────
# 1. every outbound call names its timeout
# ─────────────────────────────────────────────────────────────────────────────

def test_inference_timeouts_are_explicit():
    """Connect fails fast; read is patient, because generation is slow on CPU."""
    assert settings.inference_connect_timeout == 5.0
    assert settings.inference_read_timeout == 300.0


def test_the_proxy_uses_the_configured_timeouts():
    from api.inference_proxy import _timeout

    timeout = _timeout()
    assert timeout.connect == settings.inference_connect_timeout
    assert timeout.read == settings.inference_read_timeout
    assert timeout.write is not None and timeout.pool is not None


def test_the_agent_client_uses_the_configured_timeouts(monkeypatch):
    """The loop builds its own client when none is passed in.

    Answered from a mock transport rather than the real proxy: this asserts how
    the client is configured, and routing it to a live Ollama would make a unit
    test take a minute and fail when nothing is running.
    """
    import asyncio

    import agent.orchestrator as orch

    captured = {}
    real = httpx.AsyncClient

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "NVDA and AAPL are indexed."},
             "finish_reason": "stop"}]})

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **{**kwargs, "transport": httpx.MockTransport(answer)})

    monkeypatch.setattr(orch.httpx, "AsyncClient", spy)

    asyncio.run(orch.run_agent("q", max_iterations=1, router_model=""))

    timeout = captured["timeout"]
    assert timeout.connect == settings.inference_connect_timeout
    assert timeout.read == settings.inference_read_timeout


def test_news_timeout_is_explicit_and_split():
    """A feed that accepts the socket then stalls is the common failure, and one
    number cannot say 'fail fast on unreachable, be patient once connected'."""
    assert news_search.REQUEST_TIMEOUT == (settings.news_connect_timeout,
                                           settings.news_read_timeout)


def test_edgar_timeout_is_explicit():
    from ingestion.edgar_client import EdgarClient

    client = EdgarClient()
    try:
        assert client._client.timeout.connect == settings.edgar_connect_timeout
        assert client._client.timeout.read == settings.edgar_read_timeout
    finally:
        client.close()


def test_yfinance_timeout_is_passed_explicitly(monkeypatch):
    """yfinance defaults to 10s on history. Inheriting a default is not a
    policy: it is invisible here and changes under a pip upgrade."""
    seen = {}

    class Spy:
        def history(self, **kwargs):
            seen.update(kwargs)
            return pd.DataFrame()

    monkeypatch.setattr(yf, "Ticker", lambda symbol: Spy())
    stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")
    assert seen["timeout"] == settings.yfinance_timeout


def test_yfinance_retries_are_enabled():
    """The library ships its own retry loop disabled (retries = 0). Turning it
    on beats wrapping a second layer around it, which would multiply the two."""
    from yfinance.config import YfConfig

    import tools._yahoo  # noqa: F401  - applies the policy on import

    assert YfConfig.network.retries == settings.yfinance_retries
    assert settings.yfinance_retries >= 1


# ─────────────────────────────────────────────────────────────────────────────
# 2. the retry helper
# ─────────────────────────────────────────────────────────────────────────────

def policy(**kwargs) -> RetryPolicy:
    kwargs.setdefault("sleep", lambda _seconds: None)
    return RetryPolicy(**kwargs)


def test_a_transient_failure_is_retried_and_can_succeed():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("blip")
        return "ok"

    assert retrying(flaky, policy=policy(attempts=3)) == "ok"
    assert len(calls) == 3


def test_the_last_exception_is_re_raised_not_wrapped():
    """The tools turn this into their own envelope, and the message has to say
    what actually went wrong."""
    def always():
        raise ConnectionError("still down")

    with pytest.raises(ConnectionError, match="still down"):
        retrying(always, policy=policy(attempts=2))


def test_a_permanent_failure_is_not_retried():
    calls = []

    def missing():
        calls.append(1)
        raise FileNotFoundError("404")

    with pytest.raises(FileNotFoundError):
        retrying(missing, policy=policy(attempts=3, retry_on=(ConnectionError,)))
    assert len(calls) == 1


def test_give_up_on_wins_over_retry_on():
    calls = []

    class Permanent(ConnectionError):
        pass

    def call():
        calls.append(1)
        raise Permanent("gone")

    with pytest.raises(Permanent):
        retrying(call, policy=policy(attempts=3, retry_on=(ConnectionError,),
                                     give_up_on=(Permanent,)))
    assert len(calls) == 1


def test_a_predicate_can_decide_per_exception():
    """One exception class can carry every HTTP status, and 429 and 404 want
    opposite answers."""
    calls = []

    def call():
        calls.append(1)
        raise RuntimeError("429" if len(calls) < 2 else "404")

    with pytest.raises(RuntimeError, match="404"):
        retrying(call, policy=policy(attempts=5,
                                     predicate=lambda e: "429" in str(e)))
    assert len(calls) == 2


def test_attempts_of_one_means_no_retry():
    calls = []

    def call():
        calls.append(1)
        raise ConnectionError("x")

    with pytest.raises(ConnectionError):
        retrying(call, policy=policy(attempts=1))
    assert len(calls) == 1


def test_backoff_grows_and_stays_inside_the_ceiling():
    p = policy(base_delay=1.0, max_delay=4.0)
    for attempt in range(6):
        assert 0 <= p.delay_for(attempt) <= 4.0
    # Jittered, so compare ceilings rather than draws.
    assert min(p.delay_for(0) for _ in range(200)) < 1.0


def test_the_backoff_is_jittered():
    """Parallel tool calls that fail together must not retry together."""
    p = policy(base_delay=1.0)
    draws = {round(p.delay_for(2), 6) for _ in range(50)}
    assert len(draws) > 40


def test_zero_attempts_is_a_misconfiguration():
    with pytest.raises(ValueError, match="at least 1"):
        RetryPolicy(attempts=0)


@pytest.mark.parametrize("status,expected", [
    (429, True), (500, True), (502, True), (503, True), (408, True), (425, True),
    (200, False), (301, False), (400, False), (401, False), (403, False),
    (404, False), (422, False),
])
def test_which_statuses_are_worth_another_try(status, expected):
    assert status_is_transient(status) is expected


# ─────────────────────────────────────────────────────────────────────────────
# 3. news: retries, timeouts, garbage
# ─────────────────────────────────────────────────────────────────────────────

FEED = (b'<?xml version="1.0"?><rss><channel>'
        b'<item><title>NVDA rises - Reuters</title>'
        b'<link>https://example.invalid/1</link></item>'
        b'</channel></rss>')


def http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status}", response=response)


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Backoff is correct behaviour and a waste of test time."""
    monkeypatch.setattr("core.retry.time.sleep", lambda _s: None)


def test_a_rate_limited_feed_is_retried_and_then_succeeds(monkeypatch):
    """Google News rate-limits bursts, and the agent can issue two news
    searches in one iteration."""
    attempts = []

    def flaky(url, **kwargs):
        attempts.append(url)
        if len(attempts) < 3:
            raise http_error(429)
        response = requests.Response()
        response.status_code = 200
        response._content = FEED
        return response

    monkeypatch.setattr(news_search.requests, "get", flaky)
    result = news_search.search_news("NVDA", days_back=7)

    assert result["ok"] is True
    assert len(attempts) == 3


def test_a_missing_feed_is_not_retried(monkeypatch):
    """Asking for a 404 three times does not make it exist."""
    attempts = []

    def gone(url, **kwargs):
        attempts.append(url)
        raise http_error(404)

    monkeypatch.setattr(news_search.requests, "get", gone)
    result = news_search.search_news("NVDA")

    assert result["ok"] is False
    assert len(attempts) == 1


def test_a_feed_timeout_is_a_clean_error_dict(monkeypatch):
    def stall(url, **kwargs):
        raise requests.exceptions.ReadTimeout("timed out")

    monkeypatch.setattr(news_search.requests, "get", stall)
    result = news_search.search_news("NVDA")

    assert result["ok"] is False and result["error"] == "upstream_error"
    assert "ReadTimeout" in result["message"]
    assert "not a bad query" in result["message"]


def test_binary_garbage_from_a_feed_does_not_raise(monkeypatch):
    def garbage(url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = b"\x00\x01\x02 not xml \xff"
        return response

    monkeypatch.setattr(news_search.requests, "get", garbage)
    result = news_search.search_news("NVDA")

    assert isinstance(result, dict)
    assert result["ok"] is True and result["count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 4. yfinance: garbage shapes must not escape as raw exceptions
# ─────────────────────────────────────────────────────────────────────────────

INDEX = pd.to_datetime(["2026-08-03", "2026-08-04"]).tz_localize("UTC")


class FakeTicker:
    def __init__(self, frame=None, exc=None):
        self._frame, self._exc = frame, exc

    def history(self, **kwargs):
        if self._exc:
            raise self._exc
        return self._frame

    @property
    def fast_info(self):
        return {"currency": "USD"}

    @property
    def income_stmt(self):
        if self._exc:
            raise self._exc
        return self._frame

    @property
    def balance_sheet(self):
        if self._exc:
            raise self._exc
        return self._frame


def test_a_price_table_missing_its_columns_is_an_error_not_a_keyerror(monkeypatch):
    """Measured before the fix: KeyError: 'Open' escaped the tool, and the model
    was handed "get_stock_price raised KeyError: 'Open'" to write an answer
    from. Yahoo renaming a column must not read as a bug in the question."""
    monkeypatch.setattr(yf, "Ticker", lambda s: FakeTicker(
        pd.DataFrame({"Nonsense": [1, 2]}, index=INDEX)))
    result = stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")

    assert result["ok"] is False and result["error"] == "upstream_error"
    assert "Open" in result["message"]
    assert "not a bad request" in result["message"]


def test_a_non_table_from_yahoo_is_an_error_not_an_attributeerror(monkeypatch):
    monkeypatch.setattr(yf, "Ticker", lambda s: FakeTicker("not a dataframe"))
    result = stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")

    assert result["ok"] is False and result["error"] == "upstream_error"
    assert "str" in result["message"]


def test_unparseable_prices_are_an_error_not_a_valueerror(monkeypatch):
    columns = {c: ["x", "y"] for c in ("Open", "High", "Low", "Close", "Volume")}
    monkeypatch.setattr(yf, "Ticker", lambda s: FakeTicker(
        pd.DataFrame(columns, index=INDEX)))
    result = stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")

    assert result["ok"] is False and result["error"] == "upstream_error"
    assert "Could not read the price table" in result["message"]


def test_a_price_timeout_is_a_clean_error_dict(monkeypatch):
    monkeypatch.setattr(yf, "Ticker", lambda s: FakeTicker(
        exc=requests.exceptions.ReadTimeout("timed out")))
    result = stock_price.get_stock_price("NVDA", "2026-08-03", "2026-08-05")

    assert result["ok"] is False and result["error"] == "upstream_error"
    assert "ReadTimeout" in result["message"]


def test_a_ratio_timeout_is_a_clean_error_dict(monkeypatch):
    monkeypatch.setattr(yf, "Ticker", lambda s: FakeTicker(
        exc=requests.exceptions.ReadTimeout("timed out")))
    result = ratios.calculate_ratio("NVDA", "gross_margin", "annual")

    assert result["ok"] is False and result["error"] == "upstream_error"
    assert "ReadTimeout" in result["message"]


def test_ratio_statements_with_unexpected_rows_are_an_error_dict(monkeypatch):
    monkeypatch.setattr(yf, "Ticker", lambda s: FakeTicker(
        pd.DataFrame({"Nonsense": [1, 2]}, index=INDEX)))
    result = ratios.calculate_ratio("NVDA", "gross_margin", "annual")

    assert isinstance(result, dict) and result["ok"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 5. EDGAR: which failures are worth another attempt
# ─────────────────────────────────────────────────────────────────────────────

def edgar_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://www.sec.gov/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.parametrize("exc,retried", [
    (edgar_status_error(429), True),
    (edgar_status_error(503), True),
    (edgar_status_error(500), True),
    (edgar_status_error(404), False),
    (edgar_status_error(403), False),
    (httpx.ConnectError("refused"), True),
    (httpx.ReadTimeout("stalled"), True),
    (RuntimeError("a 403 turned into our own message"), False),
])
def test_edgar_retry_classification(exc, retried):
    from ingestion.edgar_client import _edgar_transient

    assert _edgar_transient(exc) is retried


def test_edgar_retries_go_through_the_rate_limiter(monkeypatch):
    """A retry that skipped the limiter would burst against the very rate limit
    that most likely caused the failure being retried."""
    from ingestion.edgar_client import EdgarClient

    client = EdgarClient()
    try:
        waits, gets = [], []

        monkeypatch.setattr(client._limiter, "wait", lambda: waits.append(1))
        monkeypatch.setattr(client._retry, "sleep", lambda _s: None)

        def flaky(url):
            gets.append(url)
            if len(gets) < 3:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, request=httpx.Request("GET", url))

        monkeypatch.setattr(client._client, "get", flaky)
        client._get("https://www.sec.gov/x")

        assert len(gets) == 3
        assert len(waits) == 3      # one per attempt, not one per call
    finally:
        client.close()


def test_a_bad_user_agent_is_not_retried(monkeypatch):
    """403 means the User-Agent is wrong; the message says what to change, and
    asking twice more only delays the operator seeing it."""
    from ingestion.edgar_client import EdgarClient

    client = EdgarClient()
    try:
        gets = []

        monkeypatch.setattr(client._limiter, "wait", lambda: None)
        monkeypatch.setattr(client._retry, "sleep", lambda _s: None)

        def forbidden(url):
            gets.append(url)
            return httpx.Response(403, request=httpx.Request("GET", url))

        monkeypatch.setattr(client._client, "get", forbidden)
        with pytest.raises(RuntimeError, match="User-Agent"):
            client._get("https://www.sec.gov/x")
        assert len(gets) == 1
    finally:
        client.close()
