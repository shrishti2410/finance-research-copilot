"""Tests for the eval question set.

These check the dataset is well-formed and internally consistent. They do not
check the *answers* -- those come from Postgres and the live tools via
`eval/build_questions.py`, and re-deriving them here would just be the same
query twice.
"""

import json

import pytest

from eval.datasets import (
    KNOWN_TOOLS,
    Case,
    QUESTIONS_PATH,
    by_company,
    filings_only,
    load_cases,
    load_dataset,
    mixed,
    tool_only,
)


def test_the_set_is_the_promised_size():
    """30-40 was the brief. Below 30 it stops covering the corpus; above 40 a
    full run costs more model time than anyone will spend."""
    assert 30 <= len(load_cases()) <= 40


def test_every_case_loads_with_the_documented_fields():
    for case in load_cases():
        assert case.id and case.question and case.source
        assert isinstance(case.expected_answer, float)
        assert case.tolerance > 0
        assert case.unit
        assert set(case.answerable_via) <= KNOWN_TOOLS
        assert case.company in ("NVDA", "AAPL", "NVDA+AAPL")


def test_ids_are_unique():
    ids = [c.id for c in load_cases()]
    assert len(ids) == len(set(ids))


def test_questions_are_unique():
    """Two phrasings of the same question inflate a score without adding cover."""
    questions = [c.question for c in load_cases()]
    assert len(questions) == len(set(questions))


def test_the_mix_covers_all_three_routes():
    assert len(filings_only()) >= 10
    assert len(tool_only()) >= 5
    assert len(mixed()) >= 4
    # Every case belongs to exactly one of the three.
    total = len(filings_only()) + len(tool_only()) + len(mixed())
    assert total == len(load_cases())


def test_both_companies_are_covered():
    assert len(by_company("NVDA")) >= 10
    assert len(by_company("AAPL")) >= 10


def test_no_case_depends_on_a_live_share_price_alone():
    """pe_ratio moves with today's price, so a fixed expected value for it
    would start failing the day after it was written."""
    for case in load_cases():
        assert "pe_ratio" not in case.source, (
            f"{case.id} pins a value that changes daily"
        )


def test_percent_cases_carry_a_percentage_point_tolerance():
    """Absolute in the unit -- so a percent case with tolerance 0.5 means half
    a point, not half a percent of the value."""
    for case in load_cases():
        if case.unit == "percent":
            assert 0 < case.tolerance <= 2.0, case.id


def test_price_cases_tolerate_dividend_readjustment():
    """yfinance re-adjusts historical closes when a dividend is paid, so a
    cent-level tolerance on a price would rot."""
    for case in load_cases():
        if case.unit == "USD" and "get_stock_price" in case.answerable_via:
            assert case.tolerance >= case.expected_answer * 0.015, case.id


def test_balance_sheet_cases_are_not_claimed_to_be_in_the_filings():
    """The balance sheet is not ingested. A case that says otherwise would
    score a hallucination as a pass."""
    for case in load_cases():
        if "debt_to_equity" in case.source or "current_ratio" in case.source:
            assert not case.needs_filings, (
                f"{case.id}: only calculate_ratio can answer this"
            )


def test_matching_respects_the_tolerance():
    case = Case(
        id="x", question="q", expected_answer=71.07, unit="percent",
        tolerance=0.5, answerable_via=("calculate_ratio",), company="NVDA",
        source="s",
    )
    assert case.matches(71.07)
    assert case.matches(71.5)
    assert case.matches(70.6)
    assert not case.matches(71.6)
    assert not case.matches(46.91)


def test_no_number_found_is_a_miss_not_a_pass():
    """An answer the extractor could not find a number in has not answered."""
    case = load_cases()[0]
    assert case.matches(None) is False


def test_a_duplicate_id_is_rejected(tmp_path):
    payload = load_dataset()
    payload["cases"] = [payload["cases"][0], dict(payload["cases"][0])]
    path = tmp_path / "dupe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate case id"):
        load_cases(path)


def test_an_unknown_tool_is_rejected(tmp_path):
    """A typo in answerable_via would silently drop the case out of every
    filter, quietly shrinking the set."""
    payload = load_dataset()
    payload["cases"] = [{**payload["cases"][0], "answerable_via": ["serch_filings"]}]
    path = tmp_path / "typo.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown tool"):
        load_cases(path)


def test_a_zero_tolerance_is_rejected(tmp_path):
    payload = load_dataset()
    payload["cases"] = [{**payload["cases"][0], "tolerance": 0}]
    path = tmp_path / "exact.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="tolerance must be positive"):
        load_cases(path)


def test_the_file_records_where_its_numbers_came_from():
    payload = load_dataset()
    assert payload["generated_at"]
    assert payload["tolerance_semantics"]
    assert set(payload["corpus"]) == {"NVDA", "AAPL"}
    assert QUESTIONS_PATH.exists()
