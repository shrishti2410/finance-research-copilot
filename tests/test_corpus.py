"""The step-4 corpus: its pins, SEC's paginated filing history, the CIK
override, and the resumable indexer. Offline: nothing here reaches sec.gov or a
database.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

from ingestion.corpus import (
    CORPUS_CIK_OVERRIDES,
    CORPUS_FACTS_THROUGH,
    CORPUS_FILINGS,
    CORPUS_TICKERS,
)
from ingestion.edgar_client import EdgarClient
from ingestion.pipeline import PINNED_FACTS_THROUGH, PINNED_FILINGS

ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")


# ── the pins ──────────────────────────────────────────────────────────────────

def test_fifteen_companies_three_annual_reports_each():
    assert len(CORPUS_TICKERS) == 15 and tuple(CORPUS_FILINGS) == CORPUS_TICKERS
    assert all(len(accessions) == 3 for accessions in CORPUS_FILINGS.values())
    every = [a for accessions in CORPUS_FILINGS.values() for a in accessions]
    assert len(set(every)) == 45 and all(ACCESSION.match(a) for a in every)


def test_the_corpus_contains_the_eval_filings_and_leaves_their_pins_alone():
    """The eval's corpus (one filing each for NVDA and AAPL) is separate and
    must not move with this one."""
    assert PINNED_FILINGS == {"NVDA": "0001045810-26-000021", "AAPL": "0000320193-25-000079"}
    for ticker, accession in PINNED_FILINGS.items():
        assert accession in CORPUS_FILINGS[ticker]
    assert PINNED_FACTS_THROUGH <= CORPUS_FACTS_THROUGH


def test_exxon_is_pinned_to_the_filer_of_its_10ks():
    assert CORPUS_CIK_OVERRIDES == {"XOM": "0000034088"}
    assert all(a.startswith("0000034088-") for a in CORPUS_FILINGS["XOM"])


# ── SEC's paginated history and the override ──────────────────────────────────

def _page(forms, accessions):
    n = len(forms)
    return {"form": forms, "accessionNumber": accessions,
            "filingDate": ["2025-02-01"] * n, "reportDate": ["2024-12-31"] * n,
            "primaryDocument": [f"d{i}.htm" for i in range(n)]}


RECENT = {"name": "BUSY FILER", "filings": {
    "recent": _page(["8-K", "10-K", "4"], ["0000000001-26-000003", "0000000001-26-000002",
                                           "0000000001-26-000001"]),
    "files": [{"name": "CIK0000000001-submissions-001.json"}],
}}
OLDER = _page(["10-K/A", "10-K", "8-K", "10-K"],
              ["0000000001-25-000009", "0000000001-25-000008", "0000000001-24-000007",
               "0000000001-24-000006"])


@pytest.fixture
def client(tmp_path, monkeypatch):
    cache = tmp_path / "edgar"
    cache.mkdir()
    (cache / "company_tickers.json").write_text(json.dumps({
        "0": {"cik_str": 1, "ticker": "BUSY", "title": "BUSY FILER"},
        "1": {"cik_str": 2115436, "ticker": "XOM", "title": "ExxonMobil Holdings Corp"},
    }), encoding="utf-8")
    edgar = EdgarClient(user_agent="test-suite test@example.com", cache_dir=cache,
                        require_contact=False, cik_overrides={"xom": "34088"})
    calls = []

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url: str):
        calls.append(url)
        if url.endswith("CIK0000000001.json"):
            return _Response(RECENT)
        if url.endswith("CIK0000000001-submissions-001.json"):
            return _Response(OLDER)
        if url.endswith("CIK0000034088.json"):
            return _Response({"name": "EXXON MOBIL CORP", "filings": {
                "recent": _page(["10-K"], ["0000034088-26-000045"])}})
        raise AssertionError(f"unexpected network call to {url}")

    monkeypatch.setattr(edgar, "_get", fake_get)
    edgar.calls = calls
    yield edgar
    edgar.close()


def test_older_10ks_are_read_from_the_next_page(client):
    """A company with one 10-K in its last 1,000 filings (JPMorgan, Meta and
    Exxon on 2026-11-16) still gets its last three."""
    filings = client.recent_filings("BUSY", count=3)
    assert [f.accession for f in filings] == [
        "0000000001-26-000002", "0000000001-25-000008", "0000000001-24-000006"]
    assert all(f.form == "10-K" for f in filings), "an amendment is not an annual report"
    assert filings[1].company_name == "BUSY FILER"


def test_a_page_is_fetched_only_when_needed_and_only_once(client):
    assert len(client.recent_filings("BUSY", count=1)) == 1
    assert client.calls == ["https://data.sec.gov/submissions/CIK0000000001.json"]
    client.recent_filings("BUSY", count=3)
    client.filing_by_accession("BUSY", "0000000001-24-000006")
    assert len(client.calls) == 2, "the submissions and the page are each fetched once"


def test_a_pinned_filing_on_an_older_page_is_found(client):
    filing = client.filing_by_accession("BUSY", "0000000001-24-000006")
    assert filing.form == "10-K" and filing.primary_document == "d3.htm"
    with pytest.raises(LookupError, match="in its 7 filings"):
        client.filing_by_accession("BUSY", "0000000001-23-000001")


def test_fewer_than_asked_comes_back_rather_than_an_error(client):
    assert len(client.recent_filings("BUSY", count=10)) == 3


def test_a_cik_override_wins_over_the_ticker_map(client):
    assert client.resolve_cik("XOM") == "0000034088"
    assert client.resolve_cik("BUSY") == "0000000001"
    assert client.recent_filings("XOM", count=1)[0].company_name == "EXXON MOBIL CORP"


# ── the resumable indexer ─────────────────────────────────────────────────────

def _load_indexer():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "scripts" / "index_corpus.py"
    spec = importlib.util.spec_from_file_location("index_corpus", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_rerun_skips_what_is_indexed_and_carries_on_past_a_failure(monkeypatch, tmp_path):
    indexer = _load_indexer()
    indexed = {"0000320193-25-000079": 41}          # already in the table
    attempts = []

    async def already(accession):
        return indexed.get(accession, 0)

    async def index_one(client, ticker, accession):
        attempts.append(accession)
        if accession == "0000320193-24-000123":
            raise RuntimeError("SEC returned 503")
        indexed[accession] = 10
        return {"ticker": ticker, "accession": accession, "status": "indexed",
                "fiscal_period": "FY", "chunks": 10, "embed_s": 1.0, "total_s": 1.0}

    class _Client:
        def __init__(self, **kwargs):
            assert kwargs["cik_overrides"] == CORPUS_CIK_OVERRIDES

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Conn:
        async def execute(self, *a, **k):
            class _R:
                def one(self):
                    return (len(indexed) * 10, len(indexed), 1)
            return _R()

    class _Engine:
        def connect(self):
            class _Ctx:
                async def __aenter__(self):
                    return _Conn()

                async def __aexit__(self, *exc):
                    return False
            return _Ctx()

        async def dispose(self):
            pass

    monkeypatch.setattr(indexer, "already_indexed", already)
    monkeypatch.setattr(indexer, "index_one", index_one)
    monkeypatch.setattr(indexer, "EdgarClient", _Client)
    monkeypatch.setattr(indexer, "engine", _Engine())
    report = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["index_corpus.py", "AAPL", "--no-facts",
                                      "--report", str(report)])

    assert asyncio.run(indexer.main()) == 1, "a failed filing makes the exit code non-zero"
    first = json.loads(report.read_text())
    assert [f["status"] for f in first["filings"]] == ["indexed", "skipped", "failed"]
    assert attempts == ["0000320193-26-000026", "0000320193-24-000123"]

    attempts.clear()
    assert asyncio.run(indexer.main()) == 1
    second = json.loads(report.read_text())
    assert [f["status"] for f in second["filings"]] == ["skipped", "skipped", "failed"]
    assert attempts == ["0000320193-24-000123"], "a rerun retries only what is missing"


def test_an_unknown_ticker_is_refused_before_anything_runs(monkeypatch):
    indexer = _load_indexer()
    monkeypatch.setattr(sys, "argv", ["index_corpus.py", "IBM"])
    assert asyncio.run(indexer.main()) == 2
