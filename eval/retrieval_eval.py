"""Retrieval-only evaluation: does the right chunk come back, before any model.

    python -m eval.retrieval_eval
    python -m eval.retrieval_eval --k 10 --no-rerank
    python -m eval.retrieval_eval --report retrieval.json

`search_filings` is called directly. No agent, no LLM, no tools -- so a number
here is a fact about the index and the ranker, and it is deterministic: the same
corpus gives the same score every time. That makes it the right harness for
judging a chunking or re-ranking change, which the end-to-end eval cannot do
because the model's variance swamps the difference.

Two metrics, because they answer different questions
----------------------------------------------------
Most facts in this corpus appear in more than one chunk: NVIDIA's revenue is in
the income statement *and* in the MD&A summary table, so its gold set has three
members. That makes the two metrics diverge, and neither alone is honest.

  recall@k   the share of the gold chunks that came back. Strict, and it
             punishes the retriever for not returning all three copies of a
             fact -- which is not a real failure.

  hit@k      whether at least one gold chunk came back. This is the one that
             tracks "could the question be answered from what was retrieved",
             because any single copy of the fact is sufficient.

Both are reported. hit@k is the number to optimise; recall@k is reported
because it was asked for and because a large gap between them says the ranker
is finding one copy of a fact and stopping, which is worth knowing.

No company filter is applied. Every question names its company in the text, so
filtering would be scoring an easier task than the agent actually faces.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from db.base import SessionLocal, engine
from eval.datasets import load_cases
from eval.retrieval_gold import MARKERS, GoldSet, resolve_gold
from rag.retriever import search_filings


@dataclass
class RetrievalOutcome:
    case_id: str
    question: str
    ticker: str
    marker: str
    gold: frozenset[str]
    retrieved: list[str]          # chunk ids, best first
    scores: list[float]

    def recall_at(self, k: int) -> float:
        if not self.gold:
            return 0.0
        return len(self.gold & set(self.retrieved[:k])) / len(self.gold)

    def hit_at(self, k: int) -> bool:
        return bool(self.gold & set(self.retrieved[:k]))

    @property
    def first_gold_rank(self) -> int | None:
        """1-based rank of the first gold chunk, or None if none came back."""
        for index, chunk_id in enumerate(self.retrieved, 1):
            if chunk_id in self.gold:
                return index
        return None

    @property
    def kind(self) -> str:
        """Whether the answer lives in a statement table or in prose."""
        return "table" if any("income_statement" in c for c in self.gold) else "prose"


async def evaluate(k: int, rerank: bool) -> list[RetrievalOutcome]:
    cases = {c.id: c for c in load_cases()}

    async with SessionLocal() as session:
        gold_sets: dict[str, GoldSet] = await resolve_gold(session)

        unresolved = [g.case_id for g in gold_sets.values() if not g.ok]
        if unresolved:
            raise SystemExit(
                f"gold markers matched no chunk: {unresolved}. The corpus has "
                f"changed and the markers need revisiting -- scoring against an "
                f"empty gold set would report a broken retriever."
            )

        outcomes: list[RetrievalOutcome] = []
        for case_id, gold in gold_sets.items():
            case = cases[case_id]
            hits = await search_filings(
                case.question, k=k, conn=session, rerank_results=rerank
            )
            outcomes.append(RetrievalOutcome(
                case_id=case_id,
                question=case.question,
                ticker=gold.ticker,
                marker=gold.marker,
                gold=gold.chunk_ids,
                retrieved=[h.chunk_id for h in hits],
                scores=[round(h.score, 4) for h in hits],
            ))
    await engine.dispose()
    return outcomes


def render(outcomes: list[RetrievalOutcome], k: int, rerank: bool) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 104)
    add(f"RETRIEVAL EVAL   search_filings(k={k}, rerank={rerank}), no company filter")
    add("=" * 104)
    add(f"{'':4}{'case':34} {'kind':6} {'gold':>4} {'r@3':>6} {'r@5':>6} "
        f"{'hit@3':>6} {'hit@5':>6} {'rank':>5}")
    add("-" * 104)
    for index, outcome in enumerate(sorted(outcomes, key=lambda o: o.case_id), 1):
        rank = outcome.first_gold_rank
        add(f"{index:>3} {outcome.case_id:34} {outcome.kind:6} "
            f"{len(outcome.gold):>4} "
            f"{outcome.recall_at(3):>6.2f} {outcome.recall_at(5):>6.2f} "
            f"{'yes' if outcome.hit_at(3) else 'NO':>6} "
            f"{'yes' if outcome.hit_at(5) else 'NO':>6} "
            f"{rank if rank else '-':>5}")

    def block(label: str, group: list[RetrievalOutcome]) -> None:
        if not group:
            return
        n = len(group)
        add(f"  {label:24} n={n:<3} "
            f"recall@3 {statistics.mean(o.recall_at(3) for o in group):.3f}   "
            f"recall@5 {statistics.mean(o.recall_at(5) for o in group):.3f}   "
            f"hit@3 {sum(o.hit_at(3) for o in group) / n * 100:5.1f}%   "
            f"hit@5 {sum(o.hit_at(5) for o in group) / n * 100:5.1f}%")

    add("")
    add("=" * 104)
    add("AGGREGATE")
    add("=" * 104)
    block("ALL", outcomes)
    add("")
    block("answer in a table", [o for o in outcomes if o.kind == "table"])
    block("answer in prose", [o for o in outcomes if o.kind == "prose"])
    add("")
    block("NVDA", [o for o in outcomes if o.ticker == "NVDA"])
    block("AAPL", [o for o in outcomes if o.ticker == "AAPL"])

    ranks = [o.first_gold_rank for o in outcomes if o.first_gold_rank]
    add("")
    add(f"  first gold chunk found at rank: "
        f"median {statistics.median(ranks):.0f}   mean {statistics.mean(ranks):.2f}"
        if ranks else "  no gold chunk retrieved for any question")
    add(f"  gold set size: mean {statistics.mean(len(o.gold) for o in outcomes):.2f} "
        f"chunks per question (a fact often appears in both the statement and MD&A, "
        f"which is why recall@k sits below hit@k)")

    misses = [o for o in outcomes if not o.hit_at(5)]
    if misses:
        add("")
        add("=" * 104)
        add(f"MISSES AT k=5 ({len(misses)})")
        add("=" * 104)
        for outcome in misses:
            add(f"  {outcome.case_id}")
            add(f"    asked : {outcome.question}")
            add(f"    wanted: {sorted(outcome.gold)}")
            add(f"    got   : {outcome.retrieved[:5]}")
            add("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--no-rerank", action="store_true",
                        help="pure vector search, for comparing against the "
                             "hybrid ranker")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.k < 5:
        print("--k must be at least 5 to report recall@5", file=sys.stderr)
        return 2

    outcomes = asyncio.run(evaluate(args.k, rerank=not args.no_rerank))
    report = render(outcomes, args.k, not args.no_rerank)
    print(report)

    if args.report:
        args.report.write_text(json.dumps({
            "k": args.k,
            "rerank": not args.no_rerank,
            "markers": {k: list(v) for k, v in
                        ((o.case_id, o.gold) for o in outcomes)},
            "outcomes": [
                {
                    "case_id": o.case_id, "question": o.question,
                    "ticker": o.ticker, "marker": o.marker,
                    "gold": sorted(o.gold), "retrieved": o.retrieved,
                    "scores": o.scores,
                    "recall_at_3": o.recall_at(3), "recall_at_5": o.recall_at(5),
                    "hit_at_3": o.hit_at(3), "hit_at_5": o.hit_at(5),
                    "first_gold_rank": o.first_gold_rank, "kind": o.kind,
                }
                for o in outcomes
            ],
        }, indent=2), encoding="utf-8")
        print(f"\nper-question detail written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
