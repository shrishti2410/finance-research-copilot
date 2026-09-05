"""Run one question through the agent loop and print the full trace.

    python scripts/agent_demo.py
    python scripts/agent_demo.py "What was NVDA's revenue?" --model qwen2.5:1.5b

Prints every iteration, every tool call with its arguments, and every result in
full -- the point is to see whether the tools were chosen correctly and in a
sensible order, which a final answer alone does not show.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent.orchestrator import run_agent  # noqa: E402
from agent.tools import build_registry, compact_for_model  # noqa: E402
from core.config import settings  # noqa: E402

RULE = "=" * 78

DEFAULT_QUESTION = (
    "Compare NVIDIA's gross margin to Apple's gross margin, and tell me if "
    "there's been any recent news that might explain the difference."
)


def show(label: str, value, limit: int | None = None) -> None:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    if limit and len(text) > limit:
        text = text[:limit] + f"\n... [{len(text) - limit} more chars]"
    print(f"{label}\n{text}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-iterations", type=int, default=settings.agent_max_iterations)
    ap.add_argument("--result-chars", type=int, default=1400,
                    help="cap on each printed result (0 = print in full)")
    args = ap.parse_args()

    registry = build_registry()
    model = args.model or settings.agent_model

    print(RULE)
    print(f"QUESTION: {args.question}")
    print(f"MODEL:    {model}   via {settings.agent_inference_base_url}")
    print(f"TOOLS:    {', '.join(registry.names)}")
    print(f"BUDGET:   {args.max_iterations} iterations")
    print(RULE)

    result = await run_agent(
        args.question, registry=registry, model=model, max_iterations=args.max_iterations
    )

    limit = args.result_chars or None
    for index, step in enumerate(result.steps, 1):
        print(f"\n{'─' * 78}")
        print(f"STEP {index}  |  iteration {step.iteration}  |  {step.tool}"
              f"{'' if step.ok else '   ✗ FAILED'}")
        print(f"{'─' * 78}")
        print(f"model call: {step.model_latency_ms:,.0f} ms"
              f"     tool: {step.latency_ms:,.0f} ms")

        if step.tool != "final_answer":
            show("\narguments:", step.arguments)
            show("\nresult (full):", step.result, limit)
            compact = compact_for_model(step.tool, step.result)
            if compact != step.result:
                show("\nresult as sent back to the model (compacted):", compact, limit)
        else:
            show("\nanswer:", step.result)

    print(f"\n{RULE}\nTRACE TABLE\n{RULE}")
    print(result.table())

    print(f"\n{RULE}")
    print(f"completed:   {result.completed}")
    print(f"stop_reason: {result.stop_reason}")
    print(f"iterations:  {result.iterations} of {args.max_iterations}")
    print(f"tool calls:  {len(result.tool_calls)}")
    print(f"total:       {result.total_ms / 1000:.1f}s")
    print(RULE)

    print(f"\nFINAL ANSWER\n{'-' * 78}\n{result.answer}")
    return 0 if result.completed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
