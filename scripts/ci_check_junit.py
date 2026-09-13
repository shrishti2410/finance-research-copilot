"""Fail the build when a test run skipped anything, or ran too few tests.

    python scripts/ci_check_junit.py --min-tests 900 --min-safety 12 \\
        junit-safety.xml junit-rest.xml

The first report is the `pytest -m safety` run; --min-safety applies to it.

Every external dependency of this suite -- Postgres, Redis, Ollama, the embedding
model, the indexed filings -- turns into a *skip* when it is missing, not a
failure. On a laptop that is right: someone without Ollama can still run
everything else. In CI it is exactly wrong. A runner whose Postgres service never
came up would skip the database, auth, isolation and pool-exhaustion tests and
report a green build that had tested none of them.

So here a skip is a failure. So is a count under the floor, which catches the
other way to test nothing: tests that silently stopped being collected, which no
skip count can see.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Report:
    collected: int = 0
    failed: int = 0
    skipped: int = 0
    skips: list[str] = field(default_factory=list)

    @property
    def ran(self) -> int:
        return self.collected - self.skipped


def read(path: Path) -> Report:
    """pytest writes <testsuites><testsuite ...>; older versions a bare <testsuite>."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    report = Report()
    for suite in suites:
        report.collected += int(suite.get("tests", 0))
        report.failed += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
        report.skipped += int(suite.get("skipped", 0))
        for case in suite.iter("testcase"):
            skipped = case.find("skipped")
            if skipped is not None:
                reason = (skipped.get("message") or "").strip()
                report.skips.append(f"{case.get('classname')}::{case.get('name')} -- {reason}")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("reports", nargs="+", type=Path)
    ap.add_argument("--min-tests", type=int, required=True,
                    help="tests that must actually run, across every report")
    ap.add_argument("--min-safety", type=int, default=0,
                    help="tests that must run in the first report (the -m safety run)")
    args = ap.parse_args(argv)

    problems: list[str] = []
    total_ran = 0
    for index, path in enumerate(args.reports):
        if not path.exists():
            problems.append(f"{path}: no report -- that pytest run did not happen")
            continue
        report = read(path)
        total_ran += report.ran
        print(f"{path.name}: {report.collected} collected, {report.ran} ran, "
              f"{report.skipped} skipped, {report.failed} failed or errored")
        if report.failed:
            problems.append(f"{path.name}: {report.failed} failed or errored")
        if report.skipped:
            detail = "\n    ".join(report.skips)
            problems.append(f"{path.name}: {report.skipped} skipped. In CI a skip means a "
                            f"dependency did not come up:\n    {detail}")
        if index == 0 and report.ran < args.min_safety:
            problems.append(f"{path.name}: only {report.ran} safety tests ran; the floor is "
                            f"{args.min_safety}")

    if total_ran < args.min_tests:
        problems.append(f"only {total_ran} tests ran; the floor is {args.min_tests}. If tests "
                        f"were removed on purpose, lower --min-tests in "
                        f".github/workflows/tests.yml in the same change.")

    print()
    if problems:
        for problem in problems:
            first, _, rest = problem.partition("\n")
            print(f"::error::{first}")
            if rest:
                print(rest)
        return 1
    print(f"OK: {total_ran} tests ran, none skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
