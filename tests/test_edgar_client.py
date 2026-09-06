"""Tests for the EDGAR client. Entirely offline -- no request leaves the machine.

The network calls are the one thing not worth testing against the real service:
SEC rate-limits, and a test suite that hammers edgar is exactly the behaviour the
client is written to avoid.
"""

import json
import time

import pytest

from core.config import settings
from ingestion.edgar_client import (
    ARCHIVE_URL,
    UserAgentCheck,
    validate_user_agent,
    EdgarClient,
    EdgarRateLimiter,
    Filing,
    normalize_ticker,
)

TICKER_MAP = {
    "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
}

SUBMISSIONS = {
    "name": "NVIDIA CORP",
    "filings": {
        "recent": {
            # Newest first, and deliberately seeded with a 10-K/A amendment
            # sitting above the real 10-K.
            "form": ["8-K", "10-K/A", "10-K", "10-K"],
            "accessionNumber": [
                "0001045810-26-000030",
                "0001045810-26-000025",
                "0001045810-26-000021",
                "0001045810-25-000023",
            ],
            "filingDate": ["2026-03-01", "2026-02-28", "2026-02-25", "2025-02-26"],
            "reportDate": ["2026-02-01", "2026-01-25", "2026-01-25", "2025-01-26"],
            "primaryDocument": ["x.htm", "a.htm", "nvda-20260125.htm", "nvda-20250126.htm"],
        }
    },
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    cache = tmp_path / "edgar"
    cache.mkdir()
    (cache / "company_tickers.json").write_text(json.dumps(TICKER_MAP), encoding="utf-8")

    # require_contact=False because this suite never reaches sec.gov -- `_get` is
    # stubbed below and raises on any unexpected URL. There is nobody to
    # identify ourselves to, and example.com is correctly rejected otherwise.
    edgar = EdgarClient(
        user_agent="test-suite test@example.com", cache_dir=cache, require_contact=False
    )

    class _Response:
        def __init__(self, payload):
            self._payload = payload
            self.content = json.dumps(payload).encode()

        def json(self):
            return self._payload

    def fake_get(url: str):
        if "submissions" in url:
            return _Response(SUBMISSIONS)
        raise AssertionError(f"unexpected network call to {url}")

    monkeypatch.setattr(edgar, "_get", fake_get)
    yield edgar
    edgar.close()


# ── rate limiting ────────────────────────────────────────────────────────────

def test_rate_limiter_spaces_requests():
    limiter = EdgarRateLimiter(requests_per_second=20)  # 50ms apart
    start = time.monotonic()
    for _ in range(3):
        limiter.wait()
    # Three calls means two enforced gaps. Being under this would mean the
    # client can exceed the rate SEC publishes.
    assert time.monotonic() - start >= 0.09


# ── ticker resolution ────────────────────────────────────────────────────────

def test_resolve_cik_zero_pads(client):
    # EDGAR's submissions endpoint 404s on an unpadded CIK.
    assert client.resolve_cik("NVDA") == "0001045810"
    assert client.resolve_cik("aapl") == "0000320193"


def test_resolve_cik_raises_for_unknown_ticker(client):
    with pytest.raises(LookupError):
        client.resolve_cik("NOTATICKER")


def test_normalize_ticker_converts_dots_to_dashes():
    assert normalize_ticker("brk.b") == "BRK-B"


# ── filing selection ─────────────────────────────────────────────────────────

def test_latest_filing_ignores_amendments(client):
    """A 10-K/A is a partial restatement, not the annual report."""
    filing = client.latest_filing("NVDA", form="10-K")
    assert filing.form == "10-K"
    assert filing.accession == "0001045810-26-000021"


def test_latest_filing_picks_the_most_recent(client):
    filing = client.latest_filing("NVDA")
    assert filing.filing_date.isoformat() == "2026-02-25"
    assert filing.report_date.isoformat() == "2026-01-25"


def test_latest_filing_carries_company_name(client):
    assert client.latest_filing("NVDA").company_name == "NVIDIA CORP"


def test_latest_filing_raises_when_form_absent(client):
    with pytest.raises(LookupError):
        client.latest_filing("NVDA", form="S-1")


def test_document_url_uses_unpadded_cik_and_stripped_accession(client):
    """The Archives path wants the CIK without zero padding and the accession
    without dashes, while the submissions API wants the opposite. Getting either
    backwards is a 404."""
    filing = client.latest_filing("NVDA")
    assert filing.document_url == ARCHIVE_URL.format(
        cik_int=1045810,
        accession_nodash="000104581026000021",
        document="nvda-20260125.htm",
    )
    assert "/1045810/" in filing.document_url
    assert "-" not in filing.document_url.rsplit("/", 2)[1]


# ── caching ──────────────────────────────────────────────────────────────────

def test_fetch_document_reads_cache_without_network(client, tmp_path):
    filing = client.latest_filing("NVDA")
    cache_file = client.cache_dir / "NVDA" / f"{filing.accession}.html"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("<html>cached</html>", encoding="utf-8")

    # `_get` raises on any URL but submissions, so a cache miss would fail here.
    assert client.fetch_document(filing) == "<html>cached</html>"


def test_filing_to_dict_serialises_dates():
    filing = Filing(
        ticker="NVDA", company_name="NVIDIA CORP", cik="0001045810", form="10-K",
        accession="0001045810-26-000021",
        filing_date=__import__("datetime").date(2026, 2, 25),
        report_date=__import__("datetime").date(2026, 1, 25),
        primary_document="nvda-20260125.htm", document_url="https://example.com/x.htm",
    )
    as_dict = filing.to_dict()
    assert as_dict["filing_date"] == "2026-02-25"
    assert as_dict["report_date"] == "2026-01-25"
    assert json.dumps(as_dict)  # must survive being written to the cache sidecar


# ── User-Agent validation ────────────────────────────────────────────────────
#
# SEC asks automated callers for a name and a reachable contact, and throttles
# or blocks those that do not provide one. The previous check was a substring
# test for one known placeholder and missed the placeholder that was actually in
# .env -- the default said "set SEC_EDGAR_USER_AGENT", the .env said "set a real
# address", and neither matched the other.

@pytest.mark.parametrize(
    "user_agent,why",
    [
        # The two real placeholders that shipped. Both must be caught, and the
        # old substring check caught only the first.
        ("finance-research-copilot (contact: set SEC_EDGAR_USER_AGENT)", "old default"),
        ("finance-research-copilot/0.1 (audit contact: set a real address)", "old .env"),
        ("finance-research-copilot/0.1", "no contact at all"),
        ("", "empty"),
        ("   ", "whitespace"),
        # Parses as an email and reaches nobody. RFC 2606 reserves example.*
        # precisely so that mail to it goes nowhere.
        ("test-suite test@example.com", "reserved domain"),
        ("copilot contact@example.org", "reserved domain"),
        ("copilot me@my.invalid", "reserved TLD"),
        ("copilot your@email.com", "placeholder local part"),
        ("copilot changeme@realdomain.io", "placeholder local part"),
        # Deliverable, and still not a contact: replies go nowhere by design.
        ("copilot noreply@realdomain.io", "unreachable by design"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "browser UA, no contact"),
    ],
)
def test_fake_contacts_are_rejected(user_agent, why):
    check = validate_user_agent(user_agent)
    assert not check, f"{why}: {user_agent!r} was accepted"
    assert check.reason


@pytest.mark.parametrize(
    "user_agent,contact",
    [
        ("finance-research-copilot/0.1 (someone@realdomain.io)", "someone@realdomain.io"),
        # SEC's own documented sample format.
        ("Sample Company Name AdminContact@sample-co.io", "AdminContact@sample-co.io"),
        ("copilot/0.1 (+https://github.com/someone/repo)", "https://github.com/someone/repo"),
        ("research bot ops+edgar@sub.domain.co.uk", "ops+edgar@sub.domain.co.uk"),
        # A real domain that merely contains a suspicious word. The reserved-TLD
        # check is a suffix test, not a substring test, so these must pass.
        ("copilot ops@my.test.co", "ops@my.test.co"),
        ("copilot ops+test@realdomain.io", "ops+test@realdomain.io"),
        ("copilot hello@protest.org", "hello@protest.org"),
    ],
)
def test_real_contacts_are_accepted(user_agent, contact):
    check = validate_user_agent(user_agent)
    assert check, check.reason
    assert check.contact == contact


def test_a_url_counts_as_a_contact():
    """SEC documents an email, but a project URL is a real channel and some
    crawlers use one. Accepting it keeps a personal address out of their logs."""
    assert validate_user_agent("copilot (+https://example-project.io/about)")


def test_the_check_is_falsy_so_it_reads_as_a_condition():
    assert bool(validate_user_agent("copilot ok@realdomain.io")) is True
    assert bool(validate_user_agent("copilot")) is False
    assert isinstance(validate_user_agent("x"), UserAgentCheck)


def test_client_refuses_to_start_with_a_fake_contact(tmp_path):
    """Raise at construction, not warn at request time. The old version logged
    and carried on, which is how a fake contact reached sec.gov unnoticed."""
    with pytest.raises(ValueError) as excinfo:
        EdgarClient(user_agent="copilot (set a real address)", cache_dir=tmp_path)

    message = str(excinfo.value)
    assert "no contact address" in message
    assert "SEC_EDGAR_USER_AGENT" in message      # names the setting to change
    assert "sec.gov" in message                    # and where the rule comes from


def test_client_starts_with_a_real_contact(tmp_path):
    client = EdgarClient(user_agent="copilot/0.1 (ops@realdomain.io)", cache_dir=tmp_path)
    try:
        assert client._client.headers["User-Agent"] == "copilot/0.1 (ops@realdomain.io)"
    finally:
        client.close()


def test_an_omitted_user_agent_falls_back_to_config(tmp_path, monkeypatch):
    """None means "not supplied", which is the only case config fills in."""
    monkeypatch.setattr(
        settings, "sec_edgar_user_agent", "from-config/0.1 (ops@realdomain.io)"
    )
    client = EdgarClient(cache_dir=tmp_path)
    try:
        assert client.user_agent == "from-config/0.1 (ops@realdomain.io)"
    finally:
        client.close()


def test_an_explicitly_empty_user_agent_is_its_own_failure(tmp_path, monkeypatch):
    """An empty string is a caller bug, not a request to use the configured
    value. Silently falling back would let a badly computed User-Agent sail
    through as a request that works -- under somebody else's contact."""
    monkeypatch.setattr(
        settings, "sec_edgar_user_agent", "from-config/0.1 (ops@realdomain.io)"
    )
    with pytest.raises(ValueError) as excinfo:
        EdgarClient(user_agent="", cache_dir=tmp_path)

    message = str(excinfo.value)
    assert "empty string" in message                 # says what was wrong
    assert "omit the argument" in message            # and how to get the fallback
    # Distinct from the missing-contact message, so the two are not confusable.
    assert "no contact address" not in message
    assert "from-config" not in message              # config was never consulted


def test_a_whitespace_only_user_agent_is_treated_as_empty(tmp_path):
    with pytest.raises(ValueError, match="empty string"):
        EdgarClient(user_agent="   ", cache_dir=tmp_path)


def test_an_empty_user_agent_fails_even_for_offline_callers(tmp_path):
    """require_contact governs whether a configured contact must be reachable.
    It does not make a blank argument mean something."""
    with pytest.raises(ValueError, match="empty string"):
        EdgarClient(user_agent="", cache_dir=tmp_path, require_contact=False)


def test_offline_callers_can_opt_out(tmp_path):
    """A parser run over cached files never reaches sec.gov."""
    client = EdgarClient(user_agent="offline", cache_dir=tmp_path, require_contact=False)
    client.close()
