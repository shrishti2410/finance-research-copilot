"""Structured facts: parsing SEC's company-facts JSON, storing it, and the tool.

The parsing tests run on plain dictionaries. The storage and tool tests run
against the live Postgres in DATABASE_URL, migrated to head, on a made-up
company so they do not depend on what has been indexed; they skip with a
reason when the database is not reachable. Nothing here reaches sec.gov.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from ingestion.xbrl_facts import annual_facts, fiscal_year_ends

TEST_TICKER = "ZZTESTCO"
TEST_CIK = "9999999999"


def entry(val, end, start=None, fy=None, form="10-K", filed="2026-02-25",
          accn="0009999999-26-000001", fp="FY"):
    out = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start:
        out["start"] = start
    return out


def payload(**concepts):
    """A company-facts document: concept -> (unit, [entries])."""
    return {"cik": int(TEST_CIK), "entityName": "Test Co", "facts": {"us-gaap": {
        name: {"label": name, "units": {unit: entries}}
        for name, (unit, entries) in concepts.items()}}}


# The FY2026 report prints three years; the FY2025 and FY2024 reports printed
# theirs when they were filed. A fiscal year here ends in late January.
REVENUES = ("USD", [
    entry(60, "2024-01-28", "2023-01-30", fy=2024, filed="2024-02-21", accn="0009999999-24-000001"),
    entry(60, "2024-01-28", "2023-01-30", fy=2025, filed="2025-02-26", accn="0009999999-25-000001"),
    entry(130, "2025-01-26", "2024-01-29", fy=2025, filed="2025-02-26", accn="0009999999-25-000001"),
    entry(60, "2024-01-28", "2023-01-30", fy=2026),
    entry(130, "2025-01-26", "2024-01-29", fy=2026),
    entry(215, "2026-01-25", "2025-01-27", fy=2026),
])


# ── parsing ───────────────────────────────────────────────────────────────────

def test_the_fiscal_year_comes_from_the_period_end_not_the_filing():
    """Every column of the FY2026 report carries fy=2026. Filed by `fy`, three
    different revenues would all be "fiscal 2026"."""
    rows = annual_facts(payload(Revenues=REVENUES), TEST_TICKER)
    assert {r.fiscal_year: r.value for r in rows} == {2024: 60.0, 2025: 130.0, 2026: 215.0}
    assert fiscal_year_ends(payload(Revenues=REVENUES)) == {
        date(2024, 1, 28): 2024, date(2025, 1, 26): 2025, date(2026, 1, 25): 2026}


def test_one_row_per_fiscal_year_and_it_is_the_most_recently_filed():
    """A restatement: EPS of 11.93 when filed, 1.19 in every report after a split."""
    eps = ("USD/shares", [
        entry(11.93, "2024-01-28", "2023-01-30", fy=2024, filed="2024-02-21",
              accn="0009999999-24-000001"),
        entry(1.19, "2024-01-28", "2023-01-30", fy=2026, filed="2026-02-25",
              accn="0009999999-26-000001"),
    ])
    rows = annual_facts(payload(EarningsPerShareDiluted=eps, Revenues=REVENUES), TEST_TICKER)
    row = next(r for r in rows if r.concept == "EarningsPerShareDiluted")
    assert (row.fiscal_year, row.value, row.accession) == (2024, 1.19, "0009999999-26-000001")
    assert len([r for r in rows if r.concept == "EarningsPerShareDiluted"]) == 1


def test_quarters_and_year_to_date_figures_inside_an_annual_report_are_dropped():
    with_quarter = ("USD", REVENUES[1] + [
        entry(68, "2026-01-25", "2025-10-27", fy=2026),            # fourth quarter
        entry(147, "2025-10-26", "2025-01-27", fy=2026),           # nine months
    ])
    rows = annual_facts(payload(Revenues=with_quarter), TEST_TICKER)
    assert {r.fiscal_year: r.value for r in rows} == {2024: 60.0, 2025: 130.0, 2026: 215.0}


def test_figures_from_a_quarterly_report_are_not_annual_facts():
    mixed = ("USD", REVENUES[1] + [
        entry(999, "2026-01-25", "2025-01-27", fy=2026, form="10-Q", filed="2026-03-01")])
    rows = annual_facts(payload(Revenues=mixed), TEST_TICKER)
    assert 999.0 not in {r.value for r in rows}


def test_a_balance_is_kept_when_its_date_is_a_fiscal_year_end():
    assets = ("USD", [
        entry(111, "2025-01-26", fy=2026),          # prior year end, as a comparative
        entry(206, "2026-01-25", fy=2026),          # this year end
        entry(150, "2025-07-27", fy=2026),          # a mid-year balance: not a year end
    ])
    rows = [r for r in annual_facts(payload(Revenues=REVENUES, Assets=assets), TEST_TICKER)
            if r.concept == "Assets"]
    assert {r.fiscal_year: r.value for r in rows} == {2025: 111.0, 2026: 206.0}
    assert all(r.period_start is None for r in rows)


def test_rows_carry_the_company_the_unit_and_the_filing():
    row = annual_facts(payload(Revenues=REVENUES), "zztestco")[-1]
    assert (row.cik, row.ticker, row.taxonomy, row.unit) == (TEST_CIK, TEST_TICKER, "us-gaap", "USD")
    assert (row.fiscal_period, row.form, row.filed) == ("FY", "10-K", date(2026, 2, 25))
    assert (row.period_start, row.period_end) == (date(2025, 1, 27), date(2026, 1, 25))


def test_an_empty_or_malformed_document_gives_no_rows():
    assert annual_facts({}, TEST_TICKER) == []
    assert annual_facts({"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"end": "not a date", "val": 1, "form": "10-K", "filed": "2026-02-25", "fy": 2026},
        {"end": "2026-01-25", "val": None, "form": "10-K", "filed": "2026-02-25", "fy": 2026},
    ]}}}}}, TEST_TICKER) == []


# ── the client call ───────────────────────────────────────────────────────────

def test_company_facts_goes_through_the_client_and_is_cached(tmp_path, monkeypatch):
    """One request through `_get` -- so the User-Agent rule and the rate limiter
    apply -- and none on the second call."""
    from ingestion.edgar_client import COMPANY_FACTS_URL, EdgarClient

    cache = tmp_path / "edgar"
    cache.mkdir()
    (cache / "company_tickers.json").write_text(json.dumps(
        {"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}}), encoding="utf-8")
    client = EdgarClient(user_agent="test-suite test@example.com", cache_dir=cache,
                         require_contact=False)
    asked = []

    class Response:
        def json(self):
            return {"cik": 1045810, "facts": {}}

    def fake_get(url):
        asked.append(url)
        return Response()

    monkeypatch.setattr(client, "_get", fake_get)
    assert client.company_facts("nvda") == {"cik": 1045810, "facts": {}}
    assert client.company_facts("NVDA") == {"cik": 1045810, "facts": {}}
    assert asked == [COMPANY_FACTS_URL.format(cik="0001045810")]
    client.company_facts("NVDA", use_cache=False)
    assert len(asked) == 2
    client.close()


# ── storage and the tool: live Postgres ───────────────────────────────────────

_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def close_loop():
    yield
    from db.base import engine
    _LOOP.run_until_complete(engine.dispose())
    _LOOP.close()


def _statement():
    def year(end, start, fy, **values):
        return {name: entry(val, end, None if name in INSTANTS else start, fy=fy)
                for name, val in values.items()}

    INSTANTS = {"StockholdersEquity", "AssetsCurrent", "LiabilitiesCurrent"}
    years = [
        year("2025-01-26", "2024-01-29", 2026, Revenues=130_497_000_000,
             GrossProfit=97_858_000_000, NetIncomeLoss=72_880_000_000,
             EarningsPerShareDiluted=2.94, StockholdersEquity=79_327_000_000,
             AssetsCurrent=80_126_000_000, LiabilitiesCurrent=18_047_000_000),
        year("2026-01-25", "2025-01-27", 2026, Revenues=215_938_000_000,
             GrossProfit=153_463_000_000, NetIncomeLoss=120_067_000_000,
             EarningsPerShareDiluted=4.90, StockholdersEquity=157_293_000_000,
             AssetsCurrent=125_605_000_000, LiabilitiesCurrent=32_163_000_000,
             WeightedAverageNumberOfDilutedSharesOutstanding=24_514_000_000),
    ]
    # The fiscal 2025 report, so the year label of the earlier column is 2025.
    years[0]["Revenues"] = dict(years[0]["Revenues"])
    first_report = entry(130_497_000_000, "2025-01-26", "2024-01-29", fy=2025,
                         filed="2025-02-26", accn="0009999999-25-000001")
    units = {"EarningsPerShareDiluted": "USD/shares",
             "WeightedAverageNumberOfDilutedSharesOutstanding": "shares"}
    concepts: dict = {}
    for values in years:
        for name, item in values.items():
            concepts.setdefault(name, (units.get(name, "USD"), []))[1].append(item)
    concepts["Revenues"][1].insert(0, first_report)
    return payload(**concepts)


@pytest.fixture(scope="module")
def stored():
    """The made-up company's facts in the live table, removed afterwards."""
    from sqlalchemy import delete

    from db.base import engine
    from db.models import XbrlFact
    from tools.facts import upsert_facts

    rows = annual_facts(_statement(), TEST_TICKER)

    async def setup():
        async with engine.begin() as conn:
            await conn.execute(delete(XbrlFact).where(XbrlFact.ticker == TEST_TICKER))
            return await upsert_facts(conn, rows)

    async def teardown():
        async with engine.begin() as conn:
            await conn.execute(delete(XbrlFact).where(XbrlFact.ticker == TEST_TICKER))

    try:
        written = run(setup())
    except Exception as exc:  # noqa: BLE001 - connection refused, table missing, ...
        pytest.skip(f"Postgres or the xbrl_facts table unavailable: {type(exc).__name__}")
    assert written == len(rows)
    yield rows
    run(teardown())


def fact(name, year, ticker=TEST_TICKER):
    from tools.facts import get_financial_fact
    return run(get_financial_fact(ticker, name, year))


def test_a_named_fact_for_a_fiscal_year_comes_back_with_its_filing(stored):
    result = fact("revenue", 2026)
    assert result["ok"] and result["value"] == 215_938_000_000 and result["unit"] == "USD"
    assert result["fiscal_year"] == 2026 and result["period_end"] == "2026-01-25"
    assert result["concept"] == "us-gaap:Revenues"
    assert result["accession"] == "0009999999-26-000001"
    assert result["accession"].replace("-", "") in result["source_url"]


def test_an_earlier_year_is_a_lookup_too(stored):
    result = fact("revenue", 2025)
    assert result["ok"] and result["value"] == 130_497_000_000
    assert result["period_end"] == "2025-01-26"


def test_per_share_and_share_count_facts_keep_their_units(stored):
    eps = fact("eps_diluted", 2026)
    assert (eps["value"], eps["unit"]) == (4.9, "USD/shares")
    shares = fact("shares_diluted", 2026)
    assert (shares["value"], shares["unit"]) == (24_514_000_000, "shares")


@pytest.mark.parametrize("spelling", ["Revenue", "net_sales", "total revenue", "revenues"])
def test_common_spellings_of_a_fact_name_are_accepted(stored, spelling):
    assert fact(spelling, 2026)["value"] == 215_938_000_000


@pytest.mark.parametrize("year", [2026, "2026", "FY2026", "fy 2026"])
def test_the_fiscal_year_may_arrive_as_a_string(stored, year):
    assert fact("net_income", year)["value"] == 120_067_000_000


def test_storing_the_same_facts_again_replaces_them(stored):
    from sqlalchemy import func, select

    from db.base import engine
    from db.models import XbrlFact
    from tools.facts import upsert_facts

    async def again():
        async with engine.begin() as conn:
            await upsert_facts(conn, stored)
            return (await conn.execute(select(func.count()).select_from(XbrlFact).where(
                XbrlFact.ticker == TEST_TICKER))).scalar()

    assert run(again()) == len(stored)


def test_an_unknown_fact_is_bad_input_that_lists_the_known_ones(stored):
    result = fact("services_revenue", 2026)
    assert not result["ok"] and result["error"] == "bad_input"
    assert "revenue" in result["message"] and "search_filings" in result["message"]


def test_a_year_the_company_did_not_report_lists_the_years_it_did(stored):
    result = fact("revenue", 2019)
    assert not result["ok"] and result["error"] == "no_data"
    assert result["available_fiscal_years"] == [2025, 2026]


def test_a_company_that_is_not_ingested_says_so(stored):
    result = fact("revenue", 2026, ticker="ZZNOTINGESTED")
    assert not result["ok"] and result["error"] == "no_data"
    assert "not been" in result["message"]


@pytest.mark.parametrize("year", [None, "last year", 26, True])
def test_a_fiscal_year_that_is_not_a_year_is_bad_input(stored, year):
    assert fact("revenue", year)["error"] == "bad_input"


# ── ratios for a fiscal year ──────────────────────────────────────────────────

def ratio(name, year, ticker=TEST_TICKER):
    from agent.tools import calculate_ratio
    return run(calculate_ratio(ticker, name, fiscal_year=year))


def test_a_ratio_for_a_named_fiscal_year_is_computed_from_that_years_facts(stored):
    result = ratio("gross_margin", 2025)
    assert result["ok"] and result["fiscal_year"] == 2025
    assert result["value"] == pytest.approx(97_858 / 130_497, abs=1e-6)
    assert result["formatted"] == "74.99%"
    assert result["inputs"] == {"gross_profit": 97_858_000_000, "revenue": 130_497_000_000}
    assert result["accessions"] == ["0009999999-26-000001"]


def test_current_ratio_is_a_multiple(stored):
    result = ratio("current_ratio", 2026)
    assert result["formatted"] == "3.91x"


def test_pe_ratio_has_no_fiscal_year_form_and_says_what_to_do(stored):
    result = ratio("pe_ratio", 2026)
    assert result["error"] == "unsupported"
    assert "get_stock_price" in result["message"] and "eps_diluted" in result["message"]


def test_a_ratio_for_a_year_with_no_facts_is_no_data(stored):
    assert ratio("net_margin", 2019)["error"] == "no_data"


def test_without_a_fiscal_year_the_ratio_tool_is_the_latest_period_one(monkeypatch):
    """The fiscal_year argument is an addition: omitting it changes nothing."""
    import agent.tools as agent_tools

    seen = []
    monkeypatch.setattr(agent_tools, "_latest_ratio",
                        lambda ticker, name, period: seen.append((ticker, name, period))
                        or {"ok": True, "value": 0.5})
    result = run(agent_tools.calculate_ratio("NVDA", "gross_margin"))
    assert result == {"ok": True, "value": 0.5}
    assert seen == [("NVDA", "gross_margin", "annual")]


def test_debt_to_equity_for_the_latest_fiscal_year_uses_the_latest_period_source(
        stored, monkeypatch):
    """Total debt has one source. It answers a fiscal-year question when the year
    asked for is the year that source covers, and says so when it is not."""
    import agent.tools as agent_tools

    monkeypatch.setattr(agent_tools, "_latest_ratio", lambda ticker, name, period: {
        "ok": True, "ratio": name, "value": 0.07, "period_end": "2026-01-31"})
    latest = run(agent_tools.calculate_ratio(TEST_TICKER, "debt_to_equity", fiscal_year=2026))
    assert latest["ok"] and latest["value"] == 0.07 and latest["fiscal_year"] == 2026
    earlier = run(agent_tools.calculate_ratio(TEST_TICKER, "debt_to_equity", fiscal_year=2025))
    assert earlier["error"] == "unsupported" and "long_term_debt" in earlier["message"]


# ── the agent's tool set ──────────────────────────────────────────────────────

def test_the_facts_tool_is_registered_first_and_names_its_facts():
    from agent.tools import build_registry
    from tools.facts import FACTS

    registry = build_registry()
    assert registry.names[0] == "get_financial_fact"
    schema = registry.get("get_financial_fact").schema()["function"]
    assert schema["parameters"]["required"] == ["ticker", "fact", "fiscal_year"]
    assert schema["parameters"]["properties"]["fact"]["enum"] == list(FACTS)
    ratio_schema = registry.get("calculate_ratio").schema()["function"]
    assert "fiscal_year" in ratio_schema["parameters"]["properties"]
    assert "fiscal_year" not in ratio_schema["parameters"]["required"]


def test_the_prompt_and_descriptions_route_numeric_questions_to_the_facts_tool():
    from agent.prompts import system_prompt
    from agent.tools import build_registry

    assert "get_financial_fact" in system_prompt(5)
    descriptions = {s["function"]["name"]: s["function"]["description"]
                    for s in build_registry().schemas()}
    assert "Use this first" in descriptions["get_financial_fact"]
    assert "get_financial_fact first" in descriptions["search_filings"]
    assert "fiscal_year" in descriptions["calculate_ratio"]


# ── the pin ───────────────────────────────────────────────────────────────────

def test_facts_as_of_a_date_ignore_what_was_filed_later():
    """A later report restates a year and adds a new one. As of the earlier
    date neither is visible, so a re-index after the company files again gives
    the same rows."""
    eps = ("USD/shares", [
        entry(11.93, "2024-01-28", "2023-01-30", fy=2024, filed="2024-02-21",
              accn="0009999999-24-000001"),
        entry(1.19, "2024-01-28", "2023-01-30", fy=2026, filed="2026-02-25"),
        entry(4.90, "2026-01-25", "2025-01-27", fy=2026, filed="2026-02-25"),
    ])
    document = payload(EarningsPerShareDiluted=eps)
    before = annual_facts(document, TEST_TICKER, filed_through=date(2025, 12, 31))
    assert [(r.fiscal_year, r.value) for r in before] == [(2024, 11.93)]
    after = annual_facts(document, TEST_TICKER, filed_through=date(2026, 2, 25))
    assert [(r.fiscal_year, r.value) for r in after] == [(2024, 1.19), (2026, 4.9)]
    assert annual_facts(document, TEST_TICKER) == after


def test_the_eval_companies_are_pinned_to_a_date():
    from ingestion.pipeline import PINNED_FACTS_THROUGH, PINNED_FILINGS
    assert PINNED_FACTS_THROUGH == date(2026, 11, 15)
    assert set(PINNED_FILINGS) == {"NVDA", "AAPL"}



# ── a result says what it is not ──────────────────────────────────────────────

def test_every_result_says_it_is_company_wide_and_for_the_whole_year(stored):
    """Found on the fast subset: an exact figure that is nearly what was asked
    gets stated as the answer. The result has to say what it covers."""
    result = fact("revenue", 2026)
    assert "Whole company, whole fiscal year" in result["scope"]
    assert "search_filings" in result["scope"]


def test_the_lookalike_figures_are_not_named_facts(stored):
    """Dividends and buybacks are one word away from questions this tool cannot
    answer (a quarterly rate, an authorization). Asking is an error that points
    at the filing search, not a number."""
    from tools.facts import FACTS
    for name in ("dividends_per_share", "dividends_paid", "share_repurchases"):
        assert name not in FACTS
        result = fact(name, 2026)
        assert result["error"] == "bad_input" and "search_filings" in result["message"]


def test_an_argument_the_tool_does_not_have_is_an_error_not_a_silent_drop(stored):
    """`segment="Data Center"` dropped would answer a different question."""
    from agent.tools import build_registry
    result = run(build_registry().call("get_financial_fact", {
        "ticker": TEST_TICKER, "fact": "revenue", "fiscal_year": 2026,
        "segment": "Data Center"})).result
    assert not result["ok"] and result["error"] == "bad_input"
    assert "segment" in result["message"] and "search_filings" in result["message"]


def test_other_tools_still_drop_an_unknown_argument():
    """The default is unchanged: an invented extra on another tool is ignored."""
    from tools.base import ok
    from tools.registry import Registry

    def echo(ticker: str) -> dict:
        """Echo.

        Args:
            ticker: the symbol.
        """
        return ok(ticker=ticker)

    registry = Registry()
    registry.register(echo)
    result = run(registry.call("echo", {"ticker": "NVDA", "extra": 1})).result
    assert result["ok"] and result["ignored_arguments"] == ["extra"]


def test_the_description_sends_dividends_buybacks_and_segments_to_the_filing_search():
    from agent.tools import build_registry
    description = " ".join(
        build_registry().get("get_financial_fact").schema()["function"]["description"].split())
    for phrase in ("no segment or product argument", "dividends and share repurchases",
                   "search_filings"):
        assert phrase in description


# -- a ratio name is not a fact ------------------------------------------------

def test_a_ratio_name_is_answered_as_the_ratio_with_both_readings_labelled(stored):
    """Found on the full Qwen run of 2026-11-15: "gross_margin" was a spelling of
    gross_profit, so "annual gross margin" came back as $153.463 billion where
    the question wanted 71.07%. Refusing the name made the 7B model answer
    from memory, so the result now carries both readings, labelled."""
    for name in ("gross_margin", "Gross Margin", "gross_profit_margin"):
        result = fact(name, 2025)
        assert result["ok"] and result["ratio"] == "gross_margin"
        assert result["formatted"] == "74.99%"
        assert result["inputs"]["gross_profit"] == 97_858_000_000
        assert "74.99%" in result["note"] and "$97.858 billion" in result["note"]
        assert "'gross_profit'" in result["note"] and "ratio, not a reported figure" in result["note"]
        assert result["accessions"] == ["0009999999-26-000001"]


def test_a_ratio_with_no_dollar_twin_says_only_what_it_is(stored):
    result = fact("current_ratio", 2026)
    assert result["ok"] and result["ratio"] == "current_ratio"
    assert "in dollars" not in result["note"]


@pytest.mark.parametrize("name", ["pe_ratio", "debt_to_equity"])
def test_a_ratio_with_no_fiscal_year_form_is_an_error_not_a_dollar_figure(stored, name):
    result = fact(name, 2020)
    assert not result["ok"] and "value" not in result


def test_a_ratio_name_for_a_year_with_no_facts_is_no_data(stored):
    assert fact("gross_margin", 1999)["error"] == "no_data"


def test_gross_profit_is_still_the_dollar_figure(stored):
    result = fact("gross_profit", 2025)
    assert result["ok"] and result["unit"] == "USD" and result["value"] == 97_858_000_000


def test_the_facts_tool_reads_the_year_under_the_names_the_model_sent(stored):
    from agent.tools import build_registry
    reg = build_registry()
    for arguments in ({"period_end": 2026}, {"year": "2026"},
                      {"fiscal_year": 2026, "period": "annual"}):
        result = run(reg.call("get_financial_fact", {
            "ticker": TEST_TICKER, "fact": "revenue", **arguments})).result
        assert result["ok"] and result["value"] == 215_938_000_000, arguments
    quarterly = run(reg.call("get_financial_fact", {
        "ticker": TEST_TICKER, "fact": "revenue", "fiscal_year": 2026,
        "period": "quarterly"})).result
    assert quarterly["error"] == "bad_input"
