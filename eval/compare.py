"""Did that change make the agent better or worse?

    python -m eval.compare                       # run the eval, diff against the baseline
    python -m eval.compare --report evalA.json   # diff a run already captured
    python -m eval.compare --promote evalA.json  # make that run the new baseline

The point is to replace "read 40 traces and form an impression" with a number
and a list of case ids. What it prints is the accuracy delta, the cases that
started passing, and the cases that stopped passing.

A baseline stores answers, not verdicts
---------------------------------------
Both sides are rescored with the *current* `eval/metrics.py` before anything is
compared. This is not incidental -- it is the whole reason the baseline is
trustworthy.

Run A originally scored 37.5%, and two of its "wrong" verdicts were the
extractor's fault rather than the agent's: it read NVIDIA's revenue answer as
153,463 (the gross profit figure quoted earlier in the same sentence) and read
"January 31, 2026" as a dollar amount of 31.00. If a baseline froze those
verdicts, then fixing the extractor would show up here as the *agent* improving
by two cases, and every later comparison would inherit the error. Storing the
answers and rescoring both sides means a scorer change moves both numbers
together and cancels, so what is left is the agent.

The baseline does record the accuracy it had when it was saved, and a divergence
from that is reported -- not as a failure, but because "the scorer has changed
since this baseline was taken" is something the reader needs told.

One flipped case is not a regression
------------------------------------
40 cases means one case is 2.5 points, and this eval is not deterministic: the
same system scored 38.9% and 33.3% on the first 18 cases of two consecutive runs
(`--limit 18`), differing on five of them. Live prices move, and tool results
differ between runs even at temperature 0.

So the verdict needs a band. `--min-cases` (default 2) is how many cases the net
movement has to clear before this calls it better or worse; inside the band it
says flat and still lists everything that moved. The list is the part you act
on -- a net-zero run where two cases broke and two unrelated ones were fixed is
flat by the number and absolutely worth reading.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import settings
from eval.datasets import Case, load_cases
from eval.metrics import Outcome, extract_number, summarise

BASELINE_DIR = Path(__file__).parent / "baselines"
DEFAULT_BASELINE = BASELINE_DIR / "current.json"

# How many cases the net movement must clear to be called a direction rather
# than noise. Two, because one case is 2.5 points and consecutive runs of the
# unchanged system have been measured disagreeing on more than one case.
MIN_CASES = 2


# ─────────────────────────────────────────────────────────────────────────────
# Rescoring a stored run
# ─────────────────────────────────────────────────────────────────────────────

def _as_outcome(row: dict, case: Case) -> Outcome:
    """Rescore one stored row against the case as it is defined *now*."""
    extracted = extract_number(row.get("answer") or "", case.unit)
    return Outcome(
        case_id=case.id,
        question=case.question,
        expected=case.expected_answer,
        unit=case.unit,
        tolerance=case.tolerance,
        answerable_via=case.answerable_via,
        company=case.company,
        extracted=extracted,
        answer=row.get("answer") or "",
        passed=case.matches(extracted),
        latency_ms=float(row.get("latency_ms") or 0.0),
        tool_calls=int(row.get("tool_calls") or 0),
        iterations=int(row.get("iterations") or 0),
        prompt_tokens=int(row.get("prompt_tokens") or 0),
        completion_tokens=int(row.get("completion_tokens") or 0),
        completed=bool(row.get("completed")),
        stop_reason=row.get("stop_reason") or "",
        error=row.get("error") or "",
    )


def rescore(rows: list[dict], cases: dict[str, Case] | None = None) -> list[Outcome]:
    """Score stored answers with the current extractor and current dataset.

    Rows whose case id is no longer in the dataset are dropped -- a question that
    has been removed cannot be scored, and silently keeping its old verdict would
    put a stale case in a fresh number.
    """
    cases = cases if cases is not None else {c.id: c for c in load_cases()}
    return [_as_outcome(row, cases[row["case_id"]])
            for row in rows if row.get("case_id") in cases]


def drifted_ground_truth(rows: list[dict],
                         cases: dict[str, Case] | None = None) -> list[str]:
    """Cases whose expected value or tolerance has changed since the baseline.

    Comparing against ground truth that has moved is the quiet way a baseline
    starts lying, so it is called out rather than absorbed.
    """
    cases = cases if cases is not None else {c.id: c for c in load_cases()}
    drifted = []
    for row in rows:
        case = cases.get(row.get("case_id"))
        if case is None:
            continue
        was_expected = row.get("expected")
        was_tolerance = row.get("tolerance")
        if was_expected is None and was_tolerance is None:
            continue
        if (was_expected is not None
                and abs(float(was_expected) - case.expected_answer) > 1e-9):
            drifted.append(f"{case.id}: expected {was_expected} -> "
                           f"{case.expected_answer}")
        elif (was_tolerance is not None
              and abs(float(was_tolerance) - case.tolerance) > 1e-9):
            drifted.append(f"{case.id}: tolerance {was_tolerance} -> "
                           f"{case.tolerance}")
    return drifted


# ─────────────────────────────────────────────────────────────────────────────
# The comparison
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Comparison:
    """What changed between a baseline and a run."""

    verdict: str                  # "better" | "worse" | "flat"
    baseline_accuracy: float
    current_accuracy: float
    fixed: list[str] = field(default_factory=list)       # now passing
    regressed: list[str] = field(default_factory=list)   # no longer passing
    other_changes: list[str] = field(default_factory=list)  # wrong <-> no_number
    added: list[str] = field(default_factory=list)       # not in the baseline
    removed: list[str] = field(default_factory=list)     # gone from this run
    shared: int = 0
    min_cases: int = MIN_CASES

    @property
    def delta(self) -> float:
        return self.current_accuracy - self.baseline_accuracy

    @property
    def net_cases(self) -> int:
        return len(self.fixed) - len(self.regressed)

    def exit_code(self) -> int:
        """0 unless the run got worse -- so this can gate a change in CI."""
        return 1 if self.verdict == "worse" else 0


def compare(baseline_rows: list[dict], current_rows: list[dict], *,
            min_cases: int = MIN_CASES,
            cases: dict[str, Case] | None = None) -> Comparison:
    """Diff two runs, rescoring both with the current metrics.

    Accuracy is computed over the cases the two runs share. A run that covers a
    different set of questions is not comparable overall, so additions and
    removals are reported separately rather than folded into the percentage.
    """
    cases = cases if cases is not None else {c.id: c for c in load_cases()}
    was = {o.case_id: o for o in rescore(baseline_rows, cases)}
    now = {o.case_id: o for o in rescore(current_rows, cases)}

    shared = sorted(set(was) & set(now))
    fixed, regressed, other = [], [], []
    for case_id in shared:
        before, after = was[case_id].verdict, now[case_id].verdict
        if before == after:
            continue
        if after == "pass":
            fixed.append(case_id)
        elif before == "pass":
            regressed.append(f"{case_id} ({before} -> {after})")
        else:
            other.append(f"{case_id} ({before} -> {after})")

    base_acc = summarise("baseline", [was[i] for i in shared]).accuracy
    now_acc = summarise("current", [now[i] for i in shared]).accuracy

    net = len(fixed) - len(regressed)
    if net >= min_cases:
        verdict = "better"
    elif net <= -min_cases:
        verdict = "worse"
    else:
        verdict = "flat"

    return Comparison(
        verdict=verdict,
        baseline_accuracy=base_acc,
        current_accuracy=now_acc,
        fixed=fixed,
        regressed=regressed,
        other_changes=other,
        added=sorted(set(now) - set(was)),
        removed=sorted(set(was) - set(now)),
        shared=len(shared),
        min_cases=min_cases,
    )


def by_category(rows: list[dict],
                cases: dict[str, Case] | None = None) -> dict[str, Any]:
    """Accuracy per answerable_via, so a move can be attributed."""
    outcomes = rescore(rows, cases)
    groups: dict[str, list[Outcome]] = {}
    for outcome in outcomes:
        groups.setdefault(" + ".join(outcome.answerable_via), []).append(outcome)
    return {label: summarise(label, group) for label, group in sorted(groups.items())}


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────

ARROW = {"better": "UP", "worse": "DOWN", "flat": "FLAT"}


def render(comparison: Comparison, baseline: dict, current_rows: list[dict],
           cases: dict[str, Case] | None = None) -> str:
    lines: list[str] = []
    add = lines.append
    bar = "=" * 92

    add(bar)
    add(f"ACCURACY {ARROW[comparison.verdict]}"
        if comparison.verdict != "flat" else "ACCURACY FLAT")
    add(bar)
    add(f"  baseline  {baseline.get('name', '(unnamed)')}  "
        f"taken {(baseline.get('created') or '?')[:19]}  "
        f"commit {(baseline.get('git_commit') or '?')[:12]}")
    add(f"  compared over {comparison.shared} shared case(s); "
        f"a direction needs {comparison.min_cases}+ net cases")
    add("")
    add(f"  baseline accuracy  {comparison.baseline_accuracy:6.1f}%")
    add(f"  this run           {comparison.current_accuracy:6.1f}%   "
        f"({comparison.delta:+.1f} points, {comparison.net_cases:+d} cases)")
    add("")

    saved = (baseline.get("scored_at_save") or {}).get("accuracy")
    if saved is not None and abs(saved - comparison.baseline_accuracy) > 0.05:
        add(f"  note: this baseline scored {saved:.1f}% when it was taken and "
            f"{comparison.baseline_accuracy:.1f}% under the current")
        add(f"        eval/metrics.py. The scorer has changed since. Both sides "
            f"are rescored, so the")
        add(f"        comparison above is still like-for-like.")
        add("")

    drift = drifted_ground_truth(baseline.get("cases") or [], cases)
    if drift:
        add("  WARNING: the dataset's ground truth has changed since this "
            "baseline was taken:")
        for item in drift:
            add(f"    {item}")
        add("")

    def block(title: str, items: list[str]) -> None:
        if not items:
            return
        add(f"  {title} ({len(items)})")
        for item in items:
            add(f"    {item}")
        add("")

    block("NOW PASSING", comparison.fixed)
    block("NO LONGER PASSING", comparison.regressed)
    block("changed without crossing pass/fail", comparison.other_changes)
    block("new cases, not in the baseline", comparison.added)
    block("cases the baseline had and this run did not", comparison.removed)

    if not (comparison.fixed or comparison.regressed
            or comparison.other_changes):
        add("  every shared case kept its verdict.")
        add("")

    add(bar)
    add("BY CATEGORY")
    add(bar)
    base_groups = by_category(baseline.get("cases") or [], cases)
    now_groups = by_category(current_rows, cases)
    add(f"  {'category':34} {'baseline':>14} {'now':>14} {'delta':>8}")
    add("  " + "-" * 74)
    for label in sorted(set(base_groups) | set(now_groups)):
        was = base_groups.get(label)
        now = now_groups.get(label)
        was_text = f"{was.accuracy:.1f}% ({was.passed}/{was.total})" if was else "-"
        now_text = f"{now.accuracy:.1f}% ({now.passed}/{now.total})" if now else "-"
        delta = (f"{now.accuracy - was.accuracy:+.1f}"
                 if was and now else "")
        add(f"  {label:34} {was_text:>14} {now_text:>14} {delta:>8}")

    add("")
    add(bar)
    add("COST")
    add(bar)
    base_all = summarise("b", rescore(baseline.get("cases") or [], cases))
    now_all = summarise("c", rescore(current_rows, cases))
    for field_name, label, scale, unit in (
            ("mean_latency_ms", "mean latency", 1000.0, "s"),
            ("mean_tool_calls", "tool calls per question", 1.0, ""),
            ("mean_iterations", "iterations per question", 1.0, ""),
            ("total_tokens", "total tokens", 1.0, ""),
    ):
        was = getattr(base_all, field_name) / scale
        now = getattr(now_all, field_name) / scale
        change = ((now - was) / was * 100) if was else 0.0
        add(f"  {label:26} {was:12.2f}{unit} -> {now:12.2f}{unit}  "
            f"{change:+7.1f}%")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Baseline files
# ─────────────────────────────────────────────────────────────────────────────

def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            timeout=10, check=True).stdout.strip()
    except Exception:  # noqa: BLE001 - not a git checkout, or no git
        return ""


def build_baseline(rows: list[dict], name: str, *, api: str = "",
                   note: str = "", cases: dict[str, Case] | None = None,
                   system: dict | None = None) -> dict:
    """Wrap a run's rows as a baseline, with what is needed to interpret it.

    Refuses to save a baseline none of whose cases are in the current dataset.
    That combination is always a mistake -- the wrong file, or a dataset renamed
    out from under it -- and it would otherwise be stored as a cheerful 0/0,
    which every later comparison would then read as "no cases in common".
    """
    known = cases if cases is not None else {c.id: c for c in load_cases()}
    outcomes = rescore(rows, known)
    if rows and not outcomes:
        raise ValueError(
            f"None of the {len(rows)} case(s) in this run are in the current "
            f"dataset, so there is nothing to score.\n"
            f"  run had: {', '.join(sorted(str(r.get('case_id')) for r in rows)[:5])}"
            f"{' ...' if len(rows) > 5 else ''}\n"
            f"  dataset has {len(known)} case(s), e.g. "
            f"{', '.join(sorted(known)[:5])}"
        )
    unscorable = sorted({str(r.get("case_id")) for r in rows}
                        - {o.case_id for o in outcomes})
    summary = summarise(name, outcomes)
    return {
        "name": name,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "api": api,
        "note": note,
        # The configuration the run measured. A baseline without this cannot
        # answer "better than what?" -- so when the run did not record it, that
        # is stated rather than filled in from whatever is loaded right now.
        # Promoting an older report and stamping today's settings on it would
        # describe a system that was never measured.
        "system": system or {
            "agent_model": settings.agent_model,
            "agent_router_model": settings.agent_router_model,
            "agent_max_tokens": settings.agent_max_tokens,
            "agent_max_iterations": settings.agent_max_iterations,
            "agent_history_messages": settings.agent_history_messages,
            "recorded_by_the_run": False,
        },
        # What it scored when saved. A later divergence means the scorer moved,
        # which is reported rather than hidden.
        "scored_at_save": {
            "accuracy": round(summary.accuracy, 4),
            "passed": summary.passed,
            "total": summary.total,
        },
        # Rows the current dataset no longer has a case for. Kept in "cases" so
        # nothing is lost, but excluded from every number above.
        "unscorable": unscorable,
        "cases": rows,
    }


def load_baseline(path: Path = DEFAULT_BASELINE) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"No baseline at {path}. Take one with:\n"
            f"  python -m eval.compare --promote <report.json>\n"
            f"where <report.json> is written by "
            f"`python -m eval.run_eval --report <report.json>`."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def save_baseline(baseline: dict, path: Path = DEFAULT_BASELINE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    return path


def rows_from_report(path: Path) -> tuple[list[dict], str, dict | None]:
    """Read a run_eval --report file (or a baseline file) as rows.

    Returns the rows, the API it ran against, and the configuration the run
    recorded -- None for a report written before run_eval recorded one.
    """
    body = json.loads(path.read_text(encoding="utf-8"))
    rows = body.get("outcomes") or body.get("cases") or []
    return rows, body.get("api", ""), body.get("system")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, default=None,
                        help="diff this already-captured run instead of "
                             "running the eval")
    parser.add_argument("--promote", type=Path, default=None,
                        help="save this run as the baseline and exit")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--name", default="",
                        help="name for a promoted baseline")
    parser.add_argument("--note", default="",
                        help="what this baseline measured, for the reader")
    parser.add_argument("--min-cases", type=int, default=MIN_CASES,
                        help="net cases needed to call a direction (default 2)")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--limit", type=int, default=None,
                        help="run only the first N cases")
    parser.add_argument("--out", type=Path, default=None,
                        help="write this run's per-case JSON here")
    args = parser.parse_args(argv)

    if args.promote:
        rows, api, system = rows_from_report(args.promote)
        if not rows:
            print(f"{args.promote} holds no cases", file=sys.stderr)
            return 2
        name = args.name or args.promote.stem
        baseline = build_baseline(rows, name, api=api or args.api,
                                  note=args.note, system=system)
        path = save_baseline(baseline, args.baseline)
        saved = baseline["scored_at_save"]
        print(f"baseline '{name}' saved to {path}")
        print(f"  {saved['passed']}/{saved['total']} = "
              f"{saved['accuracy']:.1f}% under the current eval/metrics.py")
        print(f"  commit {baseline['git_commit'][:12] or '(not a git checkout)'}")
        if not baseline["system"].get("recorded_by_the_run"):
            print("  note: this run did not record its own configuration, so "
                  "the 'system' block holds")
            print("        today's settings and is marked "
                  "recorded_by_the_run: false. Do not read it as")
            print("        what the run measured.")
        return 0

    baseline = load_baseline(args.baseline)

    if args.report:
        current_rows, _, _ = rows_from_report(args.report)
    else:
        # Imported here so --promote and --report work with no API running.
        from eval import run_eval

        out = args.out or Path("eval_run.json")
        argv_run = ["--api", args.api, "--report", str(out)]
        if args.limit:
            argv_run += ["--limit", str(args.limit)]
        code = run_eval.main(argv_run)
        if code != 0:
            return code
        current_rows, _, _ = rows_from_report(out)

    if not current_rows:
        print("this run produced no cases to compare", file=sys.stderr)
        return 2

    comparison = compare(baseline.get("cases") or [], current_rows,
                         min_cases=args.min_cases)
    print()
    print(render(comparison, baseline, current_rows))
    return comparison.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
