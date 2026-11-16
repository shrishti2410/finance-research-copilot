"""The agent explains a trading signal; Python makes sure the honest parts are there.

    out = await explain_signal("SBIN.NS")      # local Ollama by default

The agent is asked to explain the current signal. It calls get_trading_signal
(agent/tools.py), which returns the engine's output, the votes, the regime,
the backtest verdict and the strategy cards. It writes the explanation, and
its answer passes through the same grounding and advice guards as any chat
answer (agent/orchestrator.py, unchanged): every figure must come from the
tool, and advice is replaced by the standard redirection.

Then two checks that do not trust the model to remember:

- **The verdict first, verbatim.** The tool's backtest.statement opens every
  explanation: "The 4.9-year backtest shows no edge for SBIN.NS: ...". Every
  figure in it is from the tool. It was first added only when the model left
  "no edge" out, but the live run on 2026-11-17 showed qwen2.5:7b restating
  the verdict wrongly even when it said "no edge" ("did not outperform
  buy-and-hold" for a stock that did, on return and Sharpe, but whose timing
  missed the luck bar; "beat 5%" for 0%). So the correct sentence now always
  comes first, and `model_said_no_edge` records whether the model said it
  unprompted.
- **The disclaimer.** "Educational project, not investment advice." is
  appended when it is missing.
- **The cards it was given.** When the answer names none of the card files
  the tool supplied, a "Strategy cards used:" line lists them. Those are the
  documents the explanation was written from, so it is a source list, not a
  claim about what the model read closely.

All three are reported in the result (`added`), so a run where the model left
them out is visible rather than silently repaired. A run that did not finish
(a timeout, the iteration budget) is returned as a failure with nothing added:
decorating an error message with a verdict would make it look like an answer.

Which model: local Ollama unless told otherwise. Hosted providers need the
owner's approval (2026-11-16) and are not used here by default.
"""

from __future__ import annotations

import re
from typing import Any

from market.markets import normalize_ticker
from market.signals import DISCLAIMER

__all__ = ["explain_signal", "QUESTION", "LOCAL_PROVIDER"]

LOCAL_PROVIDER = "ollama"
QUESTION = ("Explain the current trading signal for {ticker}: what the rule engine's output "
            "is, which rules drive it and why, citing the strategy cards, and what the "
            "backtest shows.")
_CARD = re.compile(r"docs/strategies/([a-z0-9_]+)\.md")


def _signal_step(steps) -> Any:
    found = None
    for step in steps:
        if step.tool == "get_trading_signal" and step.ok and isinstance(step.result, dict) \
                and step.result.get("ok"):
            found = step
    return found


async def explain_signal(ticker: str, *, provider: str = LOCAL_PROVIDER,
                         model: str | None = None, router_model: str | None = None,
                         run=None, **agent_kwargs) -> dict:
    """Run the agent on the signal question and return the checked explanation."""
    if run is None:
        from agent.orchestrator import run_agent as run
    symbol = normalize_ticker(ticker)
    result = await run(QUESTION.format(ticker=symbol), provider=provider,
                       router_provider=provider, model=model, router_model=router_model,
                       **agent_kwargs)
    if not getattr(result, "completed", True) or \
            getattr(result, "stop_reason", "final_answer") != "final_answer":
        return {"ok": False, "ticker": symbol, "answer": result.answer,
                "error": f"The agent did not finish ({getattr(result, 'stop_reason', '?')}), "
                         f"so there is no explanation.",
                "tools": [s.tool for s in result.steps], "disclaimer": DISCLAIMER}
    step = _signal_step(result.steps)
    if step is None:
        return {"ok": False, "ticker": symbol, "answer": result.answer,
                "error": "The agent did not fetch the signal, so there is no explanation "
                         "grounded in it.",
                "tools": [s.tool for s in result.steps], "disclaimer": DISCLAIMER}

    data = step.result
    answer = (result.answer or "").strip()
    added = []
    backtest = data.get("backtest") or {}
    model_said_no_edge = "no edge" in answer.lower()
    if backtest.get("statement"):
        answer = f"{backtest['statement']}\n\n{answer}".strip()
        added.append("backtest_statement")
    supplied = sorted({c["source"] for c in data.get("cards", [])})
    cited = sorted(set(_CARD.findall(answer)))
    if supplied and not cited:
        answer = f"{answer}\n\nStrategy cards used: {', '.join(supplied)}"
        added.append("card_sources")
    if DISCLAIMER.lower() not in answer.lower():
        answer = f"{answer}\n\n{DISCLAIMER}"
        added.append("disclaimer")
    return {
        "ok": True, "ticker": data["ticker"], "as_of": data["as_of"],
        "answer": answer, "added": added, "model_said_no_edge": model_said_no_edge,
        "signal": data["signal"], "score": data["score"],
        "confidence": data["confidence"], "coverage": data["coverage"],
        "backtest": backtest, "cards_cited_by_model": cited, "cards_supplied": supplied,
        "tools": [s.tool for s in result.steps],
        "disclaimer": DISCLAIMER,
    }
