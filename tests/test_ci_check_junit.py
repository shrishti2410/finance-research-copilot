"""The CI gate that turns a skipped test into a failed build.

A green CI run that skipped the database tests is worse than a red one: it claims
something was checked that was not. These pin that the gate reads pytest's JUnit
XML the way pytest writes it, and fails on each thing it exists to catch.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "ci_check_junit.py"
_spec = importlib.util.spec_from_file_location("ci_check_junit", SCRIPT)
gate = importlib.util.module_from_spec(_spec)
# Registered before executing: @dataclass looks its own module up in sys.modules,
# and without this the whole file fails at collection.
sys.modules["ci_check_junit"] = gate
_spec.loader.exec_module(gate)


def junit(tmp_path: pathlib.Path, name: str, cases: list[str], wrapped: bool = True) -> pathlib.Path:
    """cases: 'pass', 'skip' or 'fail', one entry per test."""
    body = []
    for i, kind in enumerate(cases):
        inner = {
            "pass": "",
            "skip": '<skipped type="pytest.skip" message="Postgres unreachable: OSError"/>',
            "fail": '<failure message="assert 1 == 2"/>',
        }[kind]
        body.append(f'<testcase classname="tests.test_x" name="test_{i}">{inner}</testcase>')
    suite = (f'<testsuite name="pytest" tests="{len(cases)}" errors="0" '
             f'failures="{cases.count("fail")}" skipped="{cases.count("skip")}">'
             f'{"".join(body)}</testsuite>')
    path = tmp_path / name
    path.write_text(f"<testsuites>{suite}</testsuites>" if wrapped else suite, encoding="utf-8")
    return path


def test_a_clean_run_at_the_floor_passes(tmp_path, capsys):
    safety = junit(tmp_path, "safety.xml", ["pass"] * 3)
    rest = junit(tmp_path, "rest.xml", ["pass"] * 7)
    assert gate.main(["--min-tests", "10", "--min-safety", "3", str(safety), str(rest)]) == 0
    assert "OK: 10 tests ran, none skipped" in capsys.readouterr().out


def test_one_skip_fails_the_build_and_says_which_and_why(tmp_path, capsys):
    rest = junit(tmp_path, "rest.xml", ["pass"] * 20 + ["skip"])
    assert gate.main(["--min-tests", "1", str(rest)]) == 1
    out = capsys.readouterr().out
    assert "::error::rest.xml: 1 skipped" in out
    assert "tests.test_x::test_20 -- Postgres unreachable: OSError" in out


def test_skipped_tests_do_not_count_toward_the_floor(tmp_path, capsys):
    rest = junit(tmp_path, "rest.xml", ["pass"] * 9 + ["skip"])
    assert gate.main(["--min-tests", "10", str(rest)]) == 1
    assert "only 9 tests ran; the floor is 10" in capsys.readouterr().out


def test_too_few_tests_fails_even_when_nothing_skipped(tmp_path, capsys):
    """Tests that stopped being collected leave no skip behind to count."""
    rest = junit(tmp_path, "rest.xml", ["pass"] * 5)
    assert gate.main(["--min-tests", "900", str(rest)]) == 1
    assert "only 5 tests ran; the floor is 900" in capsys.readouterr().out


def test_the_safety_run_has_a_floor_of_its_own(tmp_path, capsys):
    safety = junit(tmp_path, "safety.xml", ["pass"])
    rest = junit(tmp_path, "rest.xml", ["pass"] * 50)
    assert gate.main(["--min-tests", "10", "--min-safety", "5", str(safety), str(rest)]) == 1
    assert "only 1 safety tests ran; the floor is 5" in capsys.readouterr().out


def test_a_missing_report_fails(tmp_path, capsys):
    rest = junit(tmp_path, "rest.xml", ["pass"] * 50)
    assert gate.main(["--min-tests", "10", str(tmp_path / "safety.xml"), str(rest)]) == 1
    assert "that pytest run did not happen" in capsys.readouterr().out


def test_failures_are_reported(tmp_path, capsys):
    rest = junit(tmp_path, "rest.xml", ["pass", "fail"])
    assert gate.main(["--min-tests", "1", str(rest)]) == 1
    assert "1 failed or errored" in capsys.readouterr().out


def test_a_bare_testsuite_root_is_read_too(tmp_path):
    rest = junit(tmp_path, "rest.xml", ["pass", "skip"], wrapped=False)
    report = gate.read(rest)
    assert (report.collected, report.ran, report.skipped) == (2, 1, 1)
