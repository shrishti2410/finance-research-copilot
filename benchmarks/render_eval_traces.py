"""Print eval traces as plain text, one case at a time.

    python benchmarks/render_eval_traces.py TRACES.json                 # every failed case
    python benchmarks/render_eval_traces.py TRACES.json --all
    python benchmarks/render_eval_traces.py TRACES.json --ids a,b,c
    python benchmarks/render_eval_traces.py TRACES.json --sample 8 --seed 20261110 --failed

Reads the file `export_eval_traces.py` writes and shows, per case: the
question, the expected answer, every step in order (tool, arguments, ok, and
the result or the discarded draft), the retrieved chunks with rank and score,
the final answer and the verdict. Nothing is summarised away: this is the
material a root-cause label has to be defended against.

stdlib only, so it can run beside an eval without loading torch or the app.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import textwrap
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILED = ("wrong", "no_number", "error")


def _wrap(text: str, indent: str = "      ", width: int = 100) -> str:
    return "\n".join(textwrap.fill(line, width=width, initial_indent=indent,
                                   subsequent_indent=indent) if line.strip() else ""
                     for line in str(text).splitlines())


def _result_text(step: dict, full: bool) -> str:
    result = step.get("result")
    if isinstance(result, dict) and step.get("tool") == "search_filings":
        hits = result.get("results") or []
        lines = [f"count={result.get('count')}"]
        for rank, hit in enumerate(hits, 1):
            excerpt = hit.get("excerpt", "")
            if not full:
                excerpt = excerpt[:240] + ("…" if len(excerpt) > 240 else "")
            lines.append(f"[{rank}] {hit.get('ticker')} {hit.get('fiscal_period')} "
                         f"{hit.get('section')} score={hit.get('score')}")
            lines.append(_wrap(excerpt, indent="          "))
        return "\n".join(lines)
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    if not full and len(text) > 1200:
        text = text[:1200] + f"… (+{len(text) - 1200} chars)"
    return text


def render_case(case: dict, full: bool) -> str:
    out = []
    out.append("=" * 100)
    out.append(f"{case['case_id']}   verdict={case['verdict']}   stop={case['stop_reason']}   "
               f"completed={case['completed']}   {case['latency_ms'] / 1000:.0f}s")
    out.append(f"Q: {case['question']}")
    out.append(f"expected: {case['expected_answer']} {case['unit']}  ±{case['tolerance']}   "
               f"extracted: {case['extracted']}   via: {', '.join(case['answerable_via'])}")
    out.append(f"source: {case.get('source')}")
    if case.get("error"):
        out.append(f"error: {case['error']}")
    agent = case.get("agent") or {}
    if agent:
        out.append(f"agent: model={agent.get('model')} iterations={agent.get('iterations')} "
                   f"tools={agent.get('tools_called')}")
    out.append("-" * 100)
    for i, step in enumerate(case.get("steps") or [], 1):
        flag = "ok " if step.get("ok") else "✗  "
        args = json.dumps(step.get("arguments"), default=str)
        out.append(f"{i:>2}. it{step.get('iteration')} {flag}{step.get('tool')}  {args}  "
                   f"[{step.get('latency_ms', 0):.0f}ms tool, {step.get('model_latency_ms', 0):.0f}ms model]")
        out.append(_wrap(_result_text(step, full)))
    out.append("-" * 100)
    out.append("FINAL ANSWER:")
    out.append(_wrap(case.get("final_answer") or "(empty)"))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("traces", type=Path)
    ap.add_argument("--all", action="store_true", help="passed cases too")
    ap.add_argument("--failed", action="store_true", help="failed cases only (default)")
    ap.add_argument("--ids", default=None, help="comma-separated case ids")
    ap.add_argument("--sample", type=int, default=None, help="draw N cases at random")
    ap.add_argument("--seed", type=int, default=None, help="seed for --sample; printed")
    ap.add_argument("--full", action="store_true", help="never truncate results")
    args = ap.parse_args(argv)

    cases = json.loads(args.traces.read_text(encoding="utf-8"))["cases"]
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c["case_id"] in wanted]
    elif not args.all:
        cases = [c for c in cases if c["verdict"] in FAILED]
    if args.sample:
        rng = random.Random(args.seed)
        cases = rng.sample(cases, min(args.sample, len(cases)))
        print(f"# sample of {len(cases)} with seed={args.seed}: "
              f"{', '.join(c['case_id'] for c in cases)}")
    for case in cases:
        print(render_case(case, args.full))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
