"""What the agent is told about which fiscal years exist.

The live demo compared NVIDIA fiscal 2026 with Apple fiscal 2025 because the
system prompt and the search_filings description still said Apple's indexed
year was 2025, from before three years per company were indexed.
"""

from __future__ import annotations

from agent.prompts import SYSTEM_PROMPT


def _text() -> str:
    return " ".join(SYSTEM_PROMPT.split())


def test_the_prompt_does_not_pin_a_company_to_one_fiscal_year():
    text = _text()
    assert "(AAPL, fiscal year 2025)" not in text
    assert "(NVDA, fiscal year 2026)" not in text


def test_the_prompt_says_to_use_each_companys_latest_year_and_to_say_when_they_differ():
    text = _text()
    assert 'fiscal_year="latest"' in text
    assert "latest" in text and "differ" in text


def test_the_filing_search_does_not_claim_one_filing_per_company():
    from agent.tools import build_registry
    description = " ".join(
        build_registry().get("search_filings").schema()["function"]["description"].split())
    assert "one 10-K each" not in description
    assert "fiscal 2025" not in description
