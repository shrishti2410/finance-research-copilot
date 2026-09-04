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

PLACEHOLDER_UA_MARKER = "set SEC_EDGAR_USER_AGENT"


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
    ) -> None:
        self.user_agent = user_agent or settings.sec_edgar_user_agent
        if PLACEHOLDER_UA_MARKER in self.user_agent:
            log.warning(
                "SEC_EDGAR_USER_AGENT is unset, using a placeholder. EDGAR asks for "
                "a real contact address and may throttle or block anonymous callers."
            )
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
