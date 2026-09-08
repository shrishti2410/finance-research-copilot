"""Groundedness: does every number in an answer trace back to that turn's evidence?

    python -m eval.judge                    # judge the most recent eval run
    python -m eval.judge --limit 5
    python -m eval.judge --report out.json

Two checks run on every answer, and reporting both is the point.

**The literal check** is deterministic. It pulls every number out of the answer,
pulls every number out of the trace (tool arguments, tool results, retrieved
chunk text), and asks whether each answer-number appears in the evidence --
allowing for the scale and format changes that are not claims of their own
(0.556025 -> 55.60%, 120067000000 -> "$120.067 billion"). It cannot be wrong
about what it checks, and it cannot see a derived value: market cap computed
from a share count and a price is genuinely grounded and literally absent.

**The judge** is a model call, and it is here for exactly that gap -- values the
answer computed rather than quoted. It reads the same evidence and rules on each
claim.

Where they disagree, that is the finding
----------------------------------------
A claim the literal check supports and the judge calls ungrounded is a judge
error, and the report says so rather than passing it on as a flag. A claim
neither supports is the real thing being looked for. This is why the judge's
verdict is not the output on its own: `eval/metrics.py` argues at length against
scoring with a model, and nothing here retracts that. The judge flags for
review; the literal check is what keeps it honest.

The judge shares a model with the system under test
---------------------------------------------------
Both are qwen2.5:7b, because it is the only model on this host. Their mistakes
are therefore correlated -- a number the agent found plausible enough to invent
is one the judge may find plausible enough to accept. That is a real weakness in
this harness, not a detail, and it is the first thing to fix when a second model
is available. Until then the literal check is the part of this that does not
share the weakness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from core.config import settings
from eval.metrics import candidates

__all__ = ["Claim", "Judgement", "evidence_numbers", "literal_support",
           "judge_answer", "render_evidence"]

# How close a number has to be to an evidence number to count as the same one.
# Relative, because the same value legitimately appears as 0.556025, 55.60 and
# 55.6 depending on where it is written.
MATCH_RELATIVE = 0.002

# Scale factors an answer may legitimately apply to an evidence value without
# making a new claim: a ratio rendered as a percentage, a figure in dollars
# quoted in millions or billions.
_RESCALINGS = (1.0, 100.0, 0.01, 1e-3, 1e-6, 1e-9, 1e3, 1e6, 1e9)

BACKSLASH = chr(92)
QUOTE = chr(34)
_FENCE = re.compile(r"^```(?:json)?|```$", re.M)
# One claim object, for salvaging a document that does not parse whole.
_CLAIM_OBJECT = re.compile(r"[{][^{}]*\"kind\"[^{}]*[}]")


@dataclass
class Claim:
    """One numeric claim in an answer, and what supports it."""

    quote: str                 # the number as written, with a little context
    value: float
    literal: bool              # appears in the evidence, allowing rescaling
    judged_grounded: bool | None = None
    basis: str = ""            # the judge's stated evidence or derivation
    kind: str = ""             # the judge's label: stated / derived / ungrounded

    @property
    def flagged(self) -> bool:
        """Ungrounded by both checks. Neither alone is enough to flag."""
        return not self.literal and self.judged_grounded is False

    @property
    def judge_disagrees(self) -> bool:
        """The judge called ungrounded something literally present."""
        return self.literal and self.judged_grounded is False


@dataclass
class Judgement:
    case_id: str
    question: str
    answer: str
    claims: list[Claim] = field(default_factory=list)
    judge_error: str = ""

    @property
    def flagged_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.flagged]

    @property
    def flagged(self) -> bool:
        return bool(self.flagged_claims)


# ── the deterministic half ──────────────────────────────────────────────────

def _walk(value) -> list[float]:
    """Every number anywhere in a nested tool result."""
    out: list[float] = []
    if isinstance(value, bool):
        return out
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, str):
        return [c.value * c.scale for c in candidates(value)]
    if isinstance(value, dict):
        for key, item in value.items():
            out.extend(_walk(key))
            out.extend(_walk(item))
        return out
    if isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_walk(item))
        return out
    return out


def evidence_numbers(trace: list[dict]) -> set[float]:
    """Every number the tools produced or were asked for, this turn."""
    numbers: set[float] = set()
    for step in trace:
        if step.get("tool") == "final_answer":
            continue
        numbers.update(_walk(step.get("arguments")))
        numbers.update(_walk(step.get("result")))
    return numbers


def literal_support(value: float, evidence: set[float]) -> bool:
    """Whether `value` is an evidence number, possibly rescaled or rounded."""
    for factor in _RESCALINGS:
        target = value * factor
        for known in evidence:
            if known == 0:
                if abs(target) < 1e-12:
                    return True
                continue
            if abs(target - known) <= abs(known) * MATCH_RELATIVE:
                return True
    return False


def answer_claims(answer: str, evidence: set[float]) -> list[Claim]:
    """Every number in the answer, marked with whether the evidence contains it."""
    claims: list[Claim] = []
    seen: set[float] = set()
    for candidate in candidates(answer):
        value = candidate.value * candidate.scale
        if candidate.is_percent:
            # A percentage is its own scale; keep the written value so "55.60%"
            # is compared as 55.60 and rescaling handles the 0.5560 form.
            value = candidate.value
        if any(abs(value - s) <= abs(s) * 1e-9 for s in seen):
            continue
        seen.add(value)
        start = max(0, candidate.start - 45)
        quote = " ".join(answer[start:candidate.start + len(candidate.text) + 25].split())
        claims.append(Claim(
            quote=quote,
            value=value,
            literal=literal_support(value, evidence),
        ))
    return claims


# ── the model half ──────────────────────────────────────────────────────────

JUDGE_SYSTEM = """\
You check whether the numbers in an answer are supported by the evidence that \
was available when it was written. You are strict and you do not use outside \
knowledge: a number you happen to know is correct is still ungrounded if the \
evidence does not contain or imply it.

For each numeric claim in the answer, decide one of:
  "stated"     the number appears in the evidence, possibly rescaled \
(0.556025 and 55.60% are the same number; 120067000000 and $120.067 billion \
are the same number)
  "derived"    the number is arithmetic over evidence numbers -- say which ones \
and what operation
  "ungrounded" neither: the evidence does not contain or imply it

Dates, fiscal years, counts of things you were asked about, and rounded \
restatements of an evidence number are not ungrounded claims.

Reply with JSON only, no prose around it. Do not quote the answer text -- \
identify each claim by its number alone:
{"claims": [{"value": "215938", "kind": "stated|derived|ungrounded", \
"basis": "which evidence value, or the arithmetic"}]}\
"""

JUDGE_USER = """\
QUESTION
{question}

EVIDENCE AVAILABLE WHEN THE ANSWER WAS WRITTEN
{evidence}

ANSWER TO CHECK
{answer}
"""


def render_evidence(trace: list[dict], limit: int = 1400) -> str:
    """The trace as the judge sees it: what was called, and what came back."""
    lines: list[str] = []
    for step in trace:
        tool = step.get("tool")
        if tool in ("final_answer", "ungrounded_answer", "tool_call_budget"):
            continue
        args = json.dumps(step.get("arguments"), default=str)
        result = json.dumps(step.get("result"), default=str)
        lines.append(f"- {tool}({args})")
        lines.append(f"  -> {result[:limit]}")
    return "\n".join(lines) if lines else "(no tool was called in this turn)"


def _balanced(text: str, start: int) -> str | None:
    """The JSON object beginning at `start`, or None if it never closes."""
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == BACKSLASH:
                escaped = True
            elif char == QUOTE:
                in_string = False
            continue
        if char == QUOTE:
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _parse_judge(content: str) -> list[dict]:
    """The judge's claims, out of a reply that may not be clean JSON.

    Three attempts, because a 7B model asked for JSON produces prose wrappers,
    markdown fences, and the occasional unescaped quote. Salvaging individual
    claim objects from a broken document beats discarding the whole judgement:
    a partial ruling still flags whatever it managed to rule on, and the report
    counts the rest as a judge failure rather than as "grounded".
    """
    text = (content or "").strip()
    if text.startswith("```"):
        text = _FENCE.sub("", text).strip()

    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in judge reply: " + repr(text[:200]))

    block = _balanced(text, start)
    if block:
        try:
            payload = json.loads(block)
            claims = payload.get("claims")
            if isinstance(claims, list):
                return claims
        except json.JSONDecodeError:
            pass

    salvaged: list[dict] = []
    for match in _CLAIM_OBJECT.finditer(text):
        try:
            salvaged.append(json.loads(match.group(0)))
        except json.JSONDecodeError:
            continue
    if salvaged:
        return salvaged
    raise ValueError("unparseable judge reply: " + repr(text[:200]))


async def judge_answer(client: httpx.AsyncClient, question: str, answer: str,
                       trace: list[dict], model: str) -> list[dict]:
    """One judging call. Temperature 0, so the same input judges the same way."""
    response = await client.post("/chat/completions", json={
        "model": model,
        "temperature": 0.0,
        "stream": False,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": JUDGE_USER.format(
                question=question, evidence=render_evidence(trace), answer=answer)},
        ],
    })
    response.raise_for_status()
    content = ((response.json().get("choices") or [{}])[0]
               .get("message") or {}).get("content") or ""
    return _parse_judge(content)


def apply_judgement(claims: list[Claim], judged: list[dict]) -> None:
    """Attach the judge's ruling to the claims the extractor found.

    Matched on the number, not on position: the judge reorders and rephrases,
    and pairing by index would attach rulings to the wrong claims.
    """
    for ruling in judged:
        raw = str(ruling.get("value", ""))
        found = candidates(raw)
        if not found:
            continue
        value = found[0].value
        kind = str(ruling.get("kind", "")).lower()
        for claim in claims:
            scales = (1.0, 100.0, 0.01)
            if any(abs(claim.value * s - value) <= max(abs(value), 1e-9) * 0.01
                   for s in scales):
                claim.judged_grounded = kind in ("stated", "derived")
                claim.kind = kind
                claim.basis = str(ruling.get("basis", ""))[:300]
                break


# ── loading what to judge ───────────────────────────────────────────────────

async def load_eval_turns(limit: int | None, hours: int) -> list[dict]:
    """The stored question/answer/trace rows from the most recent eval run."""
    from sqlalchemy import text

    from db.base import SessionLocal, engine

    async with SessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT q.content AS question, a.content AS answer, a.meta AS meta, a.id
            FROM messages a
            JOIN conversations c ON c.id = a.conversation_id
            JOIN users u ON u.id = c.user_id
            JOIN messages q ON q.conversation_id = a.conversation_id
                           AND q.role = 'user' AND q.id < a.id
            WHERE u.email LIKE 'eval-%'
              AND a.role = 'assistant'
              AND a.created_at > now() - make_interval(hours => :hours)
            ORDER BY a.id
        """), {"hours": hours})).mappings().all()
    await engine.dispose()

    turns = [dict(r) for r in rows]
    return turns[:limit] if limit else turns


# ── report ──────────────────────────────────────────────────────────────────

def render(judgements: list[Judgement]) -> str:
    lines: list[str] = []
    add = lines.append

    total_claims = sum(len(j.claims) for j in judgements)
    literal = sum(1 for j in judgements for c in j.claims if c.literal)
    flagged = [j for j in judgements if j.flagged]
    disagreements = [(j, c) for j in judgements for c in j.claims
                     if c.judge_disagrees]
    errored = [j for j in judgements if j.judge_error]

    add("=" * 100)
    add("GROUNDEDNESS")
    add("=" * 100)
    add(f"  answers judged            {len(judgements)}")
    add(f"  numeric claims found      {total_claims}")
    add(f"  literally in the evidence {literal}  "
        f"({literal / total_claims * 100:.1f}%)" if total_claims else "")
    add(f"  answers flagged           {len(flagged)}")
    if errored:
        add(f"  judge failed on           {len(errored)} answer(s)")
    add("")

    add(f"{'':4}{'case':38} {'claims':>7} {'literal':>8} {'flagged':>8}  judge")
    add("-" * 100)
    for index, j in enumerate(judgements, 1):
        lit = sum(1 for c in j.claims if c.literal)
        mark = "FLAG" if j.flagged else ("error" if j.judge_error else "ok")
        add(f"{index:>3} {j.case_id[:38]:38} {len(j.claims):>7} {lit:>8} "
            f"{len(j.flagged_claims):>8}  {mark}")

    if flagged:
        add("")
        add("=" * 100)
        add(f"FLAGGED: UNGROUNDED NUMERIC CLAIMS ({len(flagged)} answer(s))")
        add("=" * 100)
        for j in flagged:
            add(f"  {j.case_id}")
            add(f"    asked : {j.question}")
            for claim in j.flagged_claims:
                add(f"    CLAIM : \"{claim.quote}\"")
                add(f"            value {claim.value:,} -- not in the trace, "
                    f"and the judge calls it {claim.kind or 'ungrounded'}")
                if claim.basis:
                    add(f"            judge: {claim.basis}")
            add("")
    else:
        add("")
        add("  No answer had a numeric claim that both checks call ungrounded.")

    if disagreements:
        add("")
        add("=" * 100)
        add(f"JUDGE ERRORS ({len(disagreements)})")
        add("=" * 100)
        add("  Claims the judge called ungrounded that are literally in the")
        add("  trace. These are the judge being wrong, not findings.")
        add("")
        for j, claim in disagreements:
            add(f"  {j.case_id}: {claim.value:,}  \"{claim.quote[:80]}\"")

    if errored:
        add("")
        add("=" * 100)
        add(f"JUDGE FAILURES ({len(errored)})")
        add("=" * 100)
        for j in errored:
            add(f"  {j.case_id}: {j.judge_error}")

    return "\n".join(line for line in lines if line != "")


async def run(limit: int | None, hours: int, model: str,
              base_url: str) -> list[Judgement]:
    turns = await load_eval_turns(limit, hours)
    if not turns:
        raise SystemExit(
            f"no eval answers stored in the last {hours}h. Run "
            f"`python -m eval.run_eval` first."
        )

    judgements: list[Judgement] = []
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(600.0, connect=10.0),
        headers={"X-Internal-Token": settings.internal_token},
    ) as client:
        for index, turn in enumerate(turns, 1):
            meta = turn.get("meta") or {}
            trace = meta.get("trace") or []
            evidence = evidence_numbers(trace)
            claims = answer_claims(turn["answer"], evidence)
            case_id = turn["question"][:60]

            judgement = Judgement(
                case_id=case_id, question=turn["question"],
                answer=turn["answer"], claims=claims,
            )
            if claims:
                try:
                    ruling = await judge_answer(
                        client, turn["question"], turn["answer"], trace, model
                    )
                    apply_judgement(claims, ruling)
                except Exception as exc:  # noqa: BLE001 - a judge failure is data
                    judgement.judge_error = f"{type(exc).__name__}: {exc}"
            judgements.append(judgement)
            print(f"  [{index:>2}/{len(turns)}] "
                  f"{len(claims)} claim(s), "
                  f"{sum(1 for c in claims if c.literal)} literal, "
                  f"{'FLAG' if judgement.flagged else 'ok'}   {case_id[:52]}",
                  flush=True)
    return judgements


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--hours", type=int, default=6,
                        help="how far back to read stored eval answers")
    parser.add_argument("--model", default=settings.agent_model)
    parser.add_argument("--base-url", default=settings.agent_inference_base_url)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    judgements = asyncio.run(run(args.limit, args.hours, args.model, args.base_url))
    print()
    print(render(judgements))

    if args.report:
        args.report.write_text(json.dumps([
            {
                "case_id": j.case_id, "question": j.question, "answer": j.answer,
                "judge_error": j.judge_error,
                "claims": [
                    {"quote": c.quote, "value": c.value, "literal": c.literal,
                     "judged_grounded": c.judged_grounded, "kind": c.kind,
                     "basis": c.basis, "flagged": c.flagged}
                    for c in j.claims
                ],
            }
            for j in judgements
        ], indent=2), encoding="utf-8")
        print(f"\nper-claim detail written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
