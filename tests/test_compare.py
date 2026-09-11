"""Tests for the baseline comparison.

This is the thing that will be trusted to say "your change made it worse", so
its own failure modes are the expensive kind: calling noise a regression, or
crediting the agent for a scorer fix. Both are pinned here.

Cases are built by hand rather than loaded from questions.json, so a future edit
to the dataset cannot silently change what these assert.
"""

import json

import pytest

from eval.compare import (
    build_baseline,
    compare,
    drifted_ground_truth,
    load_baseline,
    render,
    rescore,
    rows_from_report,
    save_baseline,
)
from eval.datasets import Case


def case(case_id: str, expected: float, unit: str = "percent",
         tolerance: float = 0.5, via=("calculate_ratio",)) -> Case:
    return Case(
        id=case_id,
        question=f"what is {case_id}?",
        expected_answer=expected,
        unit=unit,
        tolerance=tolerance,
        answerable_via=tuple(via),
        company="NVDA",
        source="hand-built for this test",
    )


def row(case_id: str, answer: str, **over) -> dict:
    base = {
        "case_id": case_id, "answer": answer, "latency_ms": 1000.0,
        "tool_calls": 1, "iterations": 2, "prompt_tokens": 100,
        "completion_tokens": 10, "completed": True, "stop_reason": "final_answer",
        "error": "",
    }
    base.update(over)
    return base


@pytest.fixture
def cases():
    return {c.id: c for c in (
        case("a", 71.07), case("b", 46.91), case("c", 60.38), case("d", 26.92),
    )}


# ── rescoring ────────────────────────────────────────────────────────────────

def test_both_sides_are_rescored_so_a_scorer_fix_does_not_read_as_progress(cases):
    """The failure this prevents.

    Run A stored verdict "wrong" for nvda-revenue-fy2026 because the old
    extractor took the first money figure in the sentence. Freezing that verdict
    in the baseline would make fixing the extractor look like the agent gaining a
    case. Here the stored verdict is a lie in both runs, and the comparison sees
    through it to the answers.
    """
    answer = "Gross profit was 71.07%."
    rows = [row("a", answer, verdict="wrong")]
    result = compare(rows, list(rows), cases=cases)

    assert result.verdict == "flat"
    assert result.baseline_accuracy == result.current_accuracy == 100.0
    assert result.fixed == [] and result.regressed == []


def test_a_case_removed_from_the_dataset_is_dropped_not_carried(cases):
    outcomes = rescore([row("a", "71.07%"), row("gone", "1%")], cases)
    assert [o.case_id for o in outcomes] == ["a"]


def test_an_unparseable_answer_scores_as_no_number(cases):
    outcome = rescore([row("a", "I could not establish it.")], cases)[0]
    assert outcome.verdict == "no_number" and outcome.extracted is None


# ── the verdict band ─────────────────────────────────────────────────────────

def test_one_flipped_case_is_flat_not_a_regression(cases):
    """40 cases means one case is 2.5 points, and consecutive runs of the
    unchanged system have disagreed on more than one."""
    before = [row("a", "71.07%"), row("b", "46.91%"), row("c", "60.38%")]
    after = [row("a", "71.07%"), row("b", "46.91%"), row("c", "99.00%")]
    result = compare(before, after, cases=cases)

    assert result.verdict == "flat"
    assert result.regressed == ["c (pass -> wrong)"]
    assert result.net_cases == -1
    assert result.exit_code() == 0


def test_two_broken_cases_is_a_regression(cases):
    before = [row("a", "71.07%"), row("b", "46.91%"), row("c", "60.38%")]
    after = [row("a", "71.07%"), row("b", "1.00%"), row("c", "99.00%")]
    result = compare(before, after, cases=cases)

    assert result.verdict == "worse"
    assert len(result.regressed) == 2
    # Non-zero, so a CI step can gate on it.
    assert result.exit_code() == 1


def test_two_fixed_cases_is_an_improvement(cases):
    before = [row("a", "nothing"), row("b", "nothing"), row("c", "60.38%")]
    after = [row("a", "71.07%"), row("b", "46.91%"), row("c", "60.38%")]
    result = compare(before, after, cases=cases)

    assert result.verdict == "better"
    assert result.fixed == ["a", "b"]
    assert result.delta == pytest.approx(66.67, abs=0.01)
    assert result.exit_code() == 0


def test_a_wash_is_flat_but_still_lists_what_moved(cases):
    """Net zero is not "nothing happened" -- two cases broke."""
    before = [row("a", "71.07%"), row("b", "46.91%"),
              row("c", "nothing"), row("d", "nothing")]
    after = [row("a", "1.00%"), row("b", "9.00%"),
             row("c", "60.38%"), row("d", "26.92%")]
    result = compare(before, after, cases=cases)

    assert result.verdict == "flat" and result.net_cases == 0
    assert result.fixed == ["c", "d"]
    assert len(result.regressed) == 2


def test_the_band_is_configurable(cases):
    before = [row("a", "71.07%"), row("b", "46.91%")]
    after = [row("a", "1.00%"), row("b", "46.91%")]
    assert compare(before, after, cases=cases, min_cases=1).verdict == "worse"


def test_a_change_that_does_not_cross_pass_fail_is_reported_separately(cases):
    """wrong -> no_number is a real change in behaviour and not an accuracy move."""
    before = [row("a", "12.00%")]
    after = [row("a", "I could not establish it.")]
    result = compare(before, after, cases=cases)

    assert result.verdict == "flat"
    assert result.other_changes == ["a (wrong -> no_number)"]
    assert result.fixed == [] and result.regressed == []


# ── comparability ────────────────────────────────────────────────────────────

def test_accuracy_is_computed_over_shared_cases_only(cases):
    """A run covering different questions is not comparable overall."""
    before = [row("a", "71.07%"), row("b", "nothing")]
    after = [row("a", "71.07%"), row("c", "60.38%")]
    result = compare(before, after, cases=cases)

    assert result.shared == 1
    assert result.baseline_accuracy == result.current_accuracy == 100.0
    assert result.added == ["c"] and result.removed == ["b"]


def test_ground_truth_drift_is_reported(cases):
    """Comparing against an expected value that has moved is a baseline lying."""
    stale = [row("a", "71.07%", expected=99.0, tolerance=0.5)]
    drift = drifted_ground_truth(stale, cases)
    assert drift == ["a: expected 99.0 -> 71.07"]


def test_matching_ground_truth_is_not_flagged(cases):
    fresh = [row("a", "71.07%", expected=71.07, tolerance=0.5)]
    assert drifted_ground_truth(fresh, cases) == []


# ── baseline files ───────────────────────────────────────────────────────────

def test_a_baseline_records_what_it_scored_when_taken(cases):
    rows = [row("a", "71.07%"), row("b", "9.00%")]
    baseline = build_baseline(rows, "test", api="http://x", cases=cases)

    assert baseline["scored_at_save"] == {"accuracy": 50.0, "passed": 1, "total": 2}
    assert baseline["name"] == "test" and baseline["cases"] == rows
    # Enough to answer "better than what?"
    assert "agent_model" in baseline["system"]
    assert "agent_router_model" in baseline["system"]


def test_a_promoted_run_that_recorded_no_config_is_marked_as_such(cases):
    """Stamping today's settings on an older run would describe a system that was
    never measured."""
    guessed = build_baseline([row("a", "71.07%")], "t", cases=cases)
    assert guessed["system"]["recorded_by_the_run"] is False

    measured = build_baseline([row("a", "71.07%")], "t", cases=cases, system={
        "agent_model": "qwen2.5:7b", "agent_router_model": "",
        "recorded_by_the_run": True})
    assert measured["system"]["recorded_by_the_run"] is True
    assert measured["system"]["agent_router_model"] == ""


def test_a_baseline_whose_cases_are_all_gone_is_refused(cases):
    """A 0/0 baseline would read as "no cases in common" forever after."""
    with pytest.raises(ValueError, match="nothing to score"):
        build_baseline([row("vanished", "1%")], "test", cases=cases)


def test_rows_the_dataset_lost_are_kept_but_not_scored(cases):
    baseline = build_baseline([row("a", "71.07%"), row("gone", "1%")], "t",
                              cases=cases)
    assert baseline["unscorable"] == ["gone"]
    assert baseline["scored_at_save"]["total"] == 1
    assert len(baseline["cases"]) == 2      # nothing thrown away


def test_a_baseline_round_trips(tmp_path, cases):
    path = tmp_path / "b.json"
    saved = build_baseline([row("a", "71.07%")], "test", cases=cases)
    save_baseline(saved, path)
    assert load_baseline(path) == saved


def test_a_missing_baseline_says_how_to_make_one(tmp_path):
    with pytest.raises(FileNotFoundError, match="--promote"):
        load_baseline(tmp_path / "absent.json")


def test_a_report_and_a_baseline_are_both_readable_as_rows(tmp_path, cases):
    """--report takes run_eval output; --promote can also re-promote a baseline."""
    report = tmp_path / "r.json"
    report.write_text(json.dumps(
        {"api": "http://a", "outcomes": [row("a", "71.07%")]}), encoding="utf-8")
    rows, api, system = rows_from_report(report)
    assert [r["case_id"] for r in rows] == ["a"] and api == "http://a"
    assert system is None       # written before run_eval recorded one

    baseline = tmp_path / "b.json"
    save_baseline(build_baseline([row("b", "46.91%")], "t", cases=cases), baseline)
    rows, _, system = rows_from_report(baseline)
    assert [r["case_id"] for r in rows] == ["b"]
    assert system["recorded_by_the_run"] is False


# ── the report ───────────────────────────────────────────────────────────────

def test_the_report_names_the_cases_that_moved(cases):
    before = [row("a", "71.07%"), row("b", "nothing"), row("c", "60.38%")]
    after = [row("a", "1.00%"), row("b", "46.91%"), row("c", "60.38%")]
    baseline = build_baseline(before, "ref", cases=cases)
    text = render(compare(before, after, cases=cases), baseline, after, cases)

    assert "NOW PASSING" in text and "b" in text
    assert "NO LONGER PASSING" in text and "a (pass -> wrong)" in text
    assert "mean latency" in text


def test_the_report_says_when_the_scorer_has_changed_since_the_baseline(cases):
    rows = [row("a", "71.07%")]
    baseline = build_baseline(rows, "ref", cases=cases)
    # Simulate a baseline taken under a different scorer.
    baseline["scored_at_save"]["accuracy"] = 12.5
    text = render(compare(rows, rows, cases=cases), baseline, rows, cases)

    assert "scorer has changed" in text
    assert "still like-for-like" in text
