"""The open-ended eval's checks (eval/open_ended.py): no model, no API."""

from __future__ import annotations

import json
from pathlib import Path

from eval.open_ended import check

CASE = {"id": "x", "sections": ["Financials", "Stock performance", "Signal"]}
GOOD = ("**Financials**\nRevenue was $215.9 billion.\n\n## Stock performance\nUp 10%.\n"
        "Signal: the engine's output is BUY on 9 of 16 rules.\n\nEducational project, not investment advice.")


def test_a_well_formed_answer_passes():
    assert check(CASE, {"answer": GOOD, "stop_reason": "final_answer"})["passed"]


def test_each_check_fails_on_its_own():
    assert check(CASE, {"answer": GOOD, "stop_reason": "max_iterations"})["checks"]["not_exhausted"] is False
    r = check(CASE, {"answer": GOOD.replace("Signal:", "Verdict:"), "stop_reason": "final_answer"})
    assert r["missing_sections"] == ["Signal"]
    assert check(CASE, {"answer": "[Unverified: x] " + GOOD, "stop_reason": "final_answer"})["checks"]["grounded"] is False
    assert check(CASE, {"answer": GOOD, "stop_reason": "investment_advice"})["checks"]["no_advice"] is False
    assert check(CASE, {"answer": GOOD + '\n- get_financial_fact: {"ok": true}', "stop_reason": "final_answer"})["checks"]["no_raw_json"] is False
    assert check(CASE, {"answer": GOOD.replace("not investment advice", "fun"), "stop_reason": "final_answer"})["checks"]["disclaimer"] is False


def test_a_section_name_inside_a_sentence_does_not_count():
    answer = GOOD.replace("Signal: the engine's", "The engine's signal:")
    assert check(CASE, {"answer": answer, "stop_reason": "final_answer"})["missing_sections"] == ["Signal"]


def test_the_set_has_ten_cases_with_sections():
    spec = json.loads((Path(__file__).resolve().parents[1] / "eval" / "open_ended.json").read_text(encoding="utf-8"))
    assert len(spec["cases"]) == 10 and all(c["sections"] for c in spec["cases"])
    assert len({c["id"] for c in spec["cases"]}) == 10
