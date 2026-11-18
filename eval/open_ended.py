"""Run the open-ended set (eval/open_ended.json) and check each answer's shape.

    python -m eval.open_ended --report benchmarks/results/<date>-open-ended/report.json

Each question is asked once through the API, in its own conversation. There is
no expected number, so nothing here touches eval/metrics.py: the checks are on
the answer's shape and safety (see "checks" in the JSON). A case passes when
every check passes. Costs one model run per question on the API's provider.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import httpx

from eval.run_eval import DEFAULT_API, Session, ask

SET = Path(__file__).with_name("open_ended.json")
EXHAUSTED = {"max_iterations", "tool_call_budget"}


def _has_section(answer: str, name: str) -> bool:
    """A heading or a line that opens with the section's name."""
    pattern = rf"(?im)^\s*(?:#+\s*|\*\*|[-*]\s*\*\*)?{re.escape(name)}\b"
    return re.search(pattern, answer) is not None


def check(case: dict, result: dict) -> dict:
    answer = result.get("answer") or ""
    stop = result.get("stop_reason") or ""
    missing = [s for s in case["sections"] if not _has_section(answer, s)]
    checks = {
        "not_exhausted": stop not in EXHAUSTED,
        "sections": not missing,
        "grounded": stop != "unverified_figures" and "[Unverified" not in answer,
        "no_advice": stop != "investment_advice",
        "no_raw_json": '{"' not in answer and '"ok":' not in answer,
        "disclaimer": re.search(r"not investment advice", answer, re.I) is not None,
    }
    return {"id": case["id"], "passed": all(checks.values()), "checks": checks,
            "missing_sections": missing, "stop_reason": stop, "iterations": result.get("iterations"),
            "total_ms": result.get("total_ms"), "prompt_tokens": result.get("prompt_tokens"),
            "answer": answer}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--only", action="append", default=None, help="case id; repeatable")
    ap.add_argument("--timeout", type=float, default=900.0)
    args = ap.parse_args()

    spec = json.loads(SET.read_text(encoding="utf-8"))
    cases = [c for c in spec["cases"] if not args.only or c["id"] in args.only]
    client = httpx.Client(base_url=args.api, timeout=args.timeout)
    session = Session(client)
    rows = []
    for case in cases:
        started = time.perf_counter()
        try:
            row = check(case, ask(session, case["question"]))
        except httpx.HTTPError as exc:
            row = {"id": case["id"], "passed": False, "error": f"{type(exc).__name__}: {exc}"}
        row["seconds"] = round(time.perf_counter() - started, 1)
        rows.append(row)
        failed = [k for k, v in (row.get("checks") or {}).items() if not v]
        print(f"{case['id']:30} {'PASS' if row['passed'] else 'FAIL'}  {row.get('stop_reason', row.get('error', ''))}"
              f"  {', '.join(failed)}", flush=True)
    passed = sum(r["passed"] for r in rows)
    print(f"\n{passed}/{len(rows)} passed")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"set": spec["name"], "passed": passed, "of": len(rows),
                                       "cases": rows}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
