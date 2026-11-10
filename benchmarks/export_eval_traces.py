"""Join an eval run's report with the traces the API stored for it.

    python benchmarks/export_eval_traces.py REPORT.json --since 2026-11-10T12:00:00Z \
        --out benchmarks/results/<date>-eval/traces.json

`eval.run_eval` asks /ask for the trace but writes only the answer and the
verdict to its report. The trace itself is not lost: every assistant message
stores it in `messages.meta["trace"]` (up to 40 steps). This reads it back out
of Postgres for the eval account that made the run, matches each turn to its
case by question text, and writes one record per case:

    question, expected answer (+ unit, tolerance), verdict, extracted value,
    stop_reason, latency, every trace step (tool, arguments, ok, result),
    the retrieved filing chunks flattened out of search_filings results,
    and the final answer.

Read-only. Changes nothing in the database or in eval/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402

from db.base import engine  # noqa: E402
from eval.datasets import load_cases  # noqa: E402

QUERY = text("""
    SELECT u.email, c.id AS conversation_id, m.role, m.content, m.meta, m.id
    FROM users u
    JOIN conversations c ON c.user_id = u.id
    JOIN messages m ON m.conversation_id = c.id
    WHERE u.email LIKE 'eval-%@example.com' AND u.created_at >= :since
    ORDER BY m.id
""")


async def load_turns(since: datetime) -> dict[str, list[dict]]:
    """question text -> stored turns, newest last."""
    async with engine.connect() as conn:
        rows = (await conn.execute(QUERY, {"since": since})).mappings().all()
    await engine.dispose()

    by_conversation: dict[str, dict] = {}
    for row in rows:
        turn = by_conversation.setdefault(str(row["conversation_id"]),
                                          {"email": row["email"]})
        if row["role"] == "user":
            turn["question"] = row["content"]
        elif row["role"] == "assistant":
            turn["answer"] = row["content"]
            turn["meta"] = row["meta"] or {}

    turns: dict[str, list[dict]] = {}
    for turn in by_conversation.values():
        if "question" in turn and "meta" in turn:
            turns.setdefault(turn["question"], []).append(turn)
    return turns


def retrieved_chunks(steps: list[dict]) -> list[dict]:
    chunks = []
    for step in steps:
        if step.get("tool") != "search_filings" or not step.get("ok"):
            continue
        for rank, hit in enumerate((step.get("result") or {}).get("results", []), 1):
            chunks.append({
                "iteration": step.get("iteration"),
                "query": step.get("arguments", {}).get("query"),
                "filters": {k: v for k, v in step.get("arguments", {}).items()
                            if k in ("company", "section", "k")},
                "rank": rank, **hit,
            })
    return chunks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report", type=Path)
    ap.add_argument("--since", required=True,
                    help="ISO timestamp before the run started; selects its account")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    report = json.loads(args.report.read_text(encoding="utf-8"))
    cases = {c.id: c for c in load_cases()}
    turns = asyncio.run(load_turns(datetime.fromisoformat(args.since)))

    records, missing = [], []
    for outcome in report["outcomes"]:
        case = cases[outcome["case_id"]]
        stored = turns.get(case.question, [])
        turn = stored[-1] if stored else None
        if turn is None:
            missing.append(case.id)
        steps = (turn or {}).get("meta", {}).get("trace", [])
        records.append({
            "case_id": case.id,
            "question": case.question,
            "expected_answer": outcome["expected"],
            "unit": outcome["unit"],
            "tolerance": outcome["tolerance"],
            "answerable_via": outcome["answerable_via"],
            "source": getattr(case, "source", None),
            "verdict": outcome["verdict"],
            "extracted": outcome["extracted"],
            "stop_reason": outcome["stop_reason"],
            "completed": outcome["completed"],
            "latency_ms": outcome["latency_ms"],
            "error": outcome["error"],
            "agent": (turn or {}).get("meta", {}).get("agent"),
            "steps": steps,
            "retrieved_chunks": retrieved_chunks(steps),
            "final_answer": outcome["answer"],
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "report": str(args.report),
        "system": report.get("system"),
        "cases": records,
    }, indent=2, default=str), encoding="utf-8")
    print(f"{len(records)} cases written to {args.out}")
    if missing:
        print(f"no stored turn found for {len(missing)}: {', '.join(missing)}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
