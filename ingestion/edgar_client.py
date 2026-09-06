"""Fetch filings and metadata from SEC EDGAR.

Three public endpoints, no API key:

    https://www.sec.gov/files/company_tickers.json        ticker -> CIK
    https://data.sec.gov/submissions/CIK##########.json   a company's filings
    https://www.sec.gov/Archives/edgar/data/...           the documents

Two rules EDGAR actually enforces, both handled here:

  * **User-Agent.** Requests without a descriptive UA carrying a real contact
    address are refused. Set `SEC_EDGAR_USER_AGENT` in `.env` to something like
    `AcmeResearch you@acme.com` before doing anything sustained -- the built-in
    default is a placeholder and says so.
  * **Rate limit.** SEC publishes a ceiling of 10 requests/second across all of
    edgar. `_throttle` keeps us under `sec_requests_per_second` (default 5),
    measured across every request this client makes, not per endpoint.

Downloaded documents are cached under `data/edgar/` (gitignored), so re-running
the parser costs SEC nothing. A 10-K is 3-15 MB of HTML; there is no reason to
pull it twice.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

import httpx

from core.config import settings

log = logging.getLogger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{document}"

# ─────────────────────────────────────────────────────────────────────────────
# User-Agent validation
# ─────────────────────────────────────────────────────────────────────────────
#
# SEC asks every automated caller to identify itself with a name and a reachable
# contact address, and throttles or blocks callers that do not. Their documented
# sample is "Sample Company Name AdminContact@sample-company.com".
#
# This used to be a substring test for one known placeholder, and it failed
# exactly as that design implies: the shipped default said "set
# SEC_EDGAR_USER_AGENT", the .env said "set a real address", and the check
# matched neither, so requests went out with a fake contact and no warning.
# Testing for one bad string only ever catches that string.
#
# So the check is structural: there must be a contact that could actually be
# reached, and it must not be one of the well-known stand-ins for one.

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Non-greedy up to the TLD, then an optional path. Greedy matching backtracks to
# the last dot and drops the path, turning a project URL into a bare domain --
# "https://github.com/you/repo" is a contact, "https://github.com" is not.
_URL = re.compile(r"https?://[^\s()<>]+?\.[A-Za-z]{2,}(?:/[^\s()<>]*)?")

# Reserved TLDs. RFC 2606 and RFC 6761 set these aside so that nothing sent to
# them is ever delivered, which is exactly what makes them useless as a contact.
# Matched as a suffix, not a substring: "my.test.co" is somebody's real domain,
# while "my.test" is not.
_RESERVED_TLDS = (".invalid", ".test", ".local", ".localhost", ".example")

# Conventional stand-in domains. These are ordinary registrable names, so a
# substring match is right -- there is no legitimate "example.com" contact.
_FAKE_DOMAINS = (
    "example.com", "example.org", "example.net", "example.edu",
    "localhost", "domain.com", "email.com", "company.com",
    "yourcompany", "mycompany", "sample-company", "somewhere.com",
)

# Phrases that appear in an address nobody checks. "noreply" is deliberately in
# here: it is a real deliverable address and still not a contact, because the
# whole point of it is that replies go nowhere.
_FAKE_MARKERS = (
    "your@", "youremail", "your-email", "your_email", "you@",
    "changeme", "change-me", "change_me", "replaceme", "placeholder",
    "todo", "fixme", "tbd", "xxx@", "test@test", "a@a.",
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "set a real", "set_a_real", "set-a-real", "set sec_edgar_user_agent",
    "admin@admin", "none@none", "user@user", "foo@bar",
)


@dataclass(frozen=True)
class UserAgentCheck:
    """Whether a User-Agent is usable against EDGAR, and why not if it is not."""

    ok: bool
    reason: str = ""
    contact: str = ""

    def __bool__(self) -> bool:
        return self.ok


def validate_user_agent(user_agent: str) -> UserAgentCheck:
    """Check that a User-Agent carries a contact SEC could actually reach.

    Accepts an email address or an http(s) URL, since either is a real channel.
    Rejects the reserved and conventional stand-ins for one -- an address at
    example.com parses as an email and reaches nobody, which is the failure this
    is here to catch.
    """
    value = (user_agent or "").strip()
    if not value:
        return UserAgentCheck(False, "SEC_EDGAR_USER_AGENT is empty.")

    lowered = value.lower()

    email = _EMAIL.search(value)
    url = _URL.search(value)
    if not email and not url:
        return UserAgentCheck(
            False,
            "SEC_EDGAR_USER_AGENT carries no contact address. SEC asks for a name "
            "and a way to reach you, e.g. "
            "'finance-research-copilot/0.1 (you@yourdomain.com)'.",
        )

    contact = (email or url).group(0)

    for marker in _FAKE_MARKERS:
        if marker in lowered:
            return UserAgentCheck(
                False,
                f"SEC_EDGAR_USER_AGENT contains {marker!r}, which is a placeholder, "
                f"not a contact. Set a real address you monitor.",
                contact,
            )

    # The host part only: a path or a local part may legitimately contain any of
    # these words ("ops+test@realdomain.io" is a real address).
    host = contact.lower().split("@")[-1].split("//")[-1].split("/")[0].rstrip(".")

    for tld in _RESERVED_TLDS:
        if host.endswith(tld):
            return UserAgentCheck(
                False,
                f"SEC_EDGAR_USER_AGENT's contact {contact!r} uses the reserved TLD "
                f"{tld!r}, which is defined never to resolve. Set a real address "
                f"you monitor.",
                contact,
            )

    for domain in _FAKE_DOMAINS:
        if domain in host:
            return UserAgentCheck(
                False,
                f"SEC_EDGAR_USER_AGENT's contact {contact!r} uses {domain!r}, a "
                f"stand-in domain that reaches nobody. Set a real address you "
                f"monitor.",
                contact,
            )

    # A bare address satisfies the letter of SEC's request but not its intent:
    # their sample leads with who you are. Worth saying, not worth refusing.
    if value.replace(contact, "").strip(" ()<>[],;:") == "":
        log.warning(
            "SEC_EDGAR_USER_AGENT is just a contact address with no identifying "
            "name. SEC's documented format is '<name> <contact>'."
        )

    return UserAgentCheck(True, contact=contact)


@dataclass(frozen=True)
class Filing:
    """One filing, located but not yet downloaded."""

    ticker: str
    company_name: str        # EDGAR's registered entity name
    cik: str                 # zero-padded to 10 digits, as EDGAR wants it
    form: str
    accession: str           # with dashes, e.g. 0001045810-25-000023
    filing_date: date
    report_date: date | None
    primary_document: str
    document_url: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["filing_date"] = self.filing_date.isoformat()
        d["report_date"] = self.report_date.isoformat() if self.report_date else None
        return d


class EdgarRateLimiter:
    """Process-wide minimum spacing between EDGAR requests.

    Deliberately a hard sleep rather than a token bucket: a bucket would let a
    burst through after an idle period, and EDGAR's limit is about
    instantaneous rate, not average. Thread-locked because the spacing has to
    hold across every caller, not per-thread.
    """

    def __init__(self, requests_per_second: float) -> None:
        self._min_interval = 1.0 / max(requests_per_second, 0.1)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()


class EdgarClient:
    """Synchronous EDGAR reader. Ingestion is an offline batch job, so there is
    nothing to gain from async here -- the rate limiter is the bottleneck, and
    it is a sleep."""

    def __init__(
        self,
        user_agent: str | None = None,
        cache_dir: str | Path | None = None,
        requests_per_second: float | None = None,
        require_contact: bool = True,
    ) -> None:
        # None means "not supplied -- take it from config". An explicit empty
        # string is a different thing: a caller that meant to pass a User-Agent
        # and computed one badly. Folding the two together let that bug run as a
        # successful request under the configured contact, which is the failure
        # mode this whole guard exists to stop, one layer up.
        if user_agent is None:
            self.user_agent = settings.sec_edgar_user_agent
        elif not user_agent.strip():
            # Unconditional, unlike the contact check below. require_contact
            # governs whether a *configured* contact has to be reachable; it
            # does not make a blank argument mean anything.
            raise ValueError(
                "EdgarClient(user_agent=...) was given an empty string.\n"
                "  Pass a User-Agent carrying a contact, e.g.\n"
                "    EdgarClient(user_agent='finance-research-copilot/0.1 "
                "(you@yourdomain.com)')\n"
                "  or omit the argument to fall back to SEC_EDGAR_USER_AGENT "
                "from the environment.\n"
                "  An empty value is not an absent one, so it is not filled in "
                "from config."
            )
        else:
            self.user_agent = user_agent

        # Raise, not warn. The previous version logged and carried on, and the
        # result was months of requests to sec.gov carrying a fake contact that
        # nobody noticed -- a warning in a batch job's output is a warning
        # nobody reads. A bad User-Agent risks the whole IP being blocked, so it
        # is worth failing at construction, where the message is unmissable and
        # names the fix.
        check = validate_user_agent(self.user_agent)
        if require_contact and not check:
            raise ValueError(
                f"{check.reason}\n"
                f"  Currently: {self.user_agent!r}\n"
                f"  Set SEC_EDGAR_USER_AGENT in .env, e.g.\n"
                f"    SEC_EDGAR_USER_AGENT=finance-research-copilot/0.1 "
                f"(you@yourdomain.com)\n"
                f"  See https://www.sec.gov/os/webmaster-faq#developers"
            )
        if not require_contact and not check:
            # Offline callers -- the test suite, a parser run over cached files
            # -- never reach sec.gov, so there is nobody to identify to.
            log.debug("User-Agent not validated (require_contact=False): %s", check.reason)

        self.cache_dir = Path(cache_dir or settings.edgar_cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._limiter = EdgarRateLimiter(
            requests_per_second or settings.sec_requests_per_second
        )
        self._client = httpx.Client(
            headers={
                "User-Agent": self.user_agent,
                # EDGAR serves gzip; a 10-K compresses about 10:1.
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=httpx.Timeout(connect=10, read=60, write=30, pool=30),
            follow_redirects=True,
        )
        self._ticker_map: dict[str, str] | None = None

    # ── plumbing ────────────────────────────────────────────────────────────

    def _get(self, url: str) -> httpx.Response:
        self._limiter.wait()
        response = self._client.get(url)
        if response.status_code == 403:
            raise RuntimeError(
                f"EDGAR returned 403 for {url}. This is almost always the User-Agent: "
                f"set SEC_EDGAR_USER_AGENT to 'YourName your@email.com'. "
                f"Currently sending: {self.user_agent!r}"
            )
        response.raise_for_status()
        return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── ticker -> CIK ───────────────────────────────────────────────────────

    def _load_ticker_map(self) -> dict[str, str]:
        """Ticker -> zero-padded CIK, cached on disk and in memory.

        The whole map is one ~1 MB file covering every listed company, which is
        cheaper than a lookup request per ticker and is what SEC publishes it for.
        """
        if self._ticker_map is not None:
            return self._ticker_map

        cache_file = self.cache_dir / "company_tickers.json"
        if cache_file.exists():
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            raw = self._get(TICKER_MAP_URL).json()
            cache_file.write_text(json.dumps(raw), encoding="utf-8")

        # Shape is {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        self._ticker_map = {
            entry["ticker"].upper(): str(entry["cik_str"]).zfill(10)
            for entry in raw.values()
        }
        return self._ticker_map

    def resolve_cik(self, ticker: str) -> str:
        mapping = self._load_ticker_map()
        cik = mapping.get(ticker.strip().upper())
        if cik is None:
            raise LookupError(f"No CIK found for ticker {ticker!r} in SEC's ticker map.")
        return cik

    # ── locating a filing ───────────────────────────────────────────────────

    def latest_filing(self, ticker: str, form: str = "10-K") -> Filing:
        """Most recent filing of `form` for `ticker`.

        `filings.recent` holds parallel arrays sorted newest-first, so the first
        exact form match is the latest. The match is exact on purpose: a
        substring test would happily return a 10-K/A amendment, which is a
        partial restatement and not the document you want to parse.
        """
        cik = self.resolve_cik(ticker)
        submissions = self._get(SUBMISSIONS_URL.format(cik=cik)).json()
        recent = submissions.get("filings", {}).get("recent", {})

        forms = recent.get("form", [])
        for i, filed_form in enumerate(forms):
            if filed_form != form:
                continue

            accession = recent["accessionNumber"][i]
            primary_document = recent["primaryDocument"][i]
            report_date = recent.get("reportDate", [None] * len(forms))[i]

            return Filing(
                ticker=ticker.upper(),
                company_name=submissions.get("name", ticker.upper()),
                cik=cik,
                form=filed_form,
                accession=accession,
                filing_date=date.fromisoformat(recent["filingDate"][i]),
                report_date=date.fromisoformat(report_date) if report_date else None,
                primary_document=primary_document,
                document_url=ARCHIVE_URL.format(
                    cik_int=int(cik),  # the Archives path uses the CIK unpadded
                    accession_nodash=accession.replace("-", ""),
                    document=primary_document,
                ),
            )

        raise LookupError(
            f"No {form} found for {ticker} in the {len(forms)} most recent filings. "
            f"Older filings live behind the paginated `filings.files` list."
        )

    # ── downloading ─────────────────────────────────────────────────────────

    def fetch_document(self, filing: Filing, use_cache: bool = True) -> str:
        """Download the filing's primary document, caching it on disk.

        Returned as text. EDGAR serves these as UTF-8 but older documents lie
        about their encoding, so decoding errors are replaced rather than raised
        -- a mojibaked character somewhere in a 10 MB filing should not abort an
        ingestion run.
        """
        cache_file = self.cache_dir / filing.ticker.upper() / f"{filing.accession}.html"

        if use_cache and cache_file.exists():
            log.debug("cache hit: %s", cache_file)
            return cache_file.read_text(encoding="utf-8", errors="replace")

        response = self._get(filing.document_url)
        html = response.content.decode("utf-8", errors="replace")

        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(html, encoding="utf-8")
        (cache_file.with_suffix(".json")).write_text(
            json.dumps(filing.to_dict(), indent=2), encoding="utf-8"
        )
        return html


def normalize_ticker(raw: str) -> str:
    """EDGAR uses dashes where market data feeds often use dots (BRK.B -> BRK-B)."""
    return re.sub(r"\.", "-", raw.strip().upper())
