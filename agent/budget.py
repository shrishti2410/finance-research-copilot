"""Keep every request to the model under a token budget.

Groq's free tier rejects any request larger than its 8,000 tokens-a-minute
limit with a 413, and two questions in the 2026-11-17 calculator run were lost
that way (estimated 8,100 and 8,250 tokens;
benchmarks/HOSTED_RUN_2026-11-17_CALCULATOR.md). So before each call the
request's size is estimated, and when it is over `settings.agent_request_token_budget`
(7,000 by default; 0 turns the budget off) the largest tool results are
trimmed until it fits:

- a result keeps its shape: lists lose items from the end (search hits are
  ranked, so the best are kept), then long strings are shortened;
- the trimmed result says what was dropped (`_trimmed`), so the model knows it
  has not seen everything and can search more narrowly;
- trimming works on the compact result *before* it is wrapped as untrusted
  content, and the result is wrapped again, so the wrapper's delimiters are
  never cut (agent/untrusted.py);
- only the copy sent to the model is trimmed. The tool's full result stays in
  the turn's steps, and the grounding guard checks figures against that.

The size is an estimate from length, calibrated against what Groq actually
counted. All 108 model calls of the 2026-11-17 calculator run were rebuilt from
their traces and compared with the prompt_tokens Groq reported for each:
- the messages run at about 4.9 characters a token, and the tool schemas at
  far more than their JSON suggests (the provider renders them compactly):
  12,875 characters of schema cost about 2,100 tokens;
- one rate for everything (3.5) over-estimated by 1.41 to 1.90 times (median
  1.60), so a 7,000 budget would have trimmed at a real 4,400 tokens;
- `CHARS_PER_TOKEN` for the messages and `TOOL_CHARS_PER_TOKEN` for the
  schemas over-estimate by 1.09 to 1.58 times (median 1.18) and never
  under-estimated a call. Under the 7,000 budget a real request is then at
  most about 6,420 tokens.
No tokenizer is needed at run time. Every call's estimate is logged and kept in the turn's trace
(`AgentResult.request_sizes`), so a 413 risk is measured, not reconstructed.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable

CHARS_PER_TOKEN = 3.5
TOOL_CHARS_PER_TOKEN = 6.0
# A result is never trimmed below this, however tight the budget: a result cut
# to nothing helps no one, and the request then goes out over budget, logged.
MIN_RESULT_TOKENS = 150


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def request_tokens(messages: list[dict], tools: list[dict]) -> int:
    tools_text = json.dumps(tools, ensure_ascii=False, default=str)
    return estimate_tokens(messages) + math.ceil(len(tools_text) / TOOL_CHARS_PER_TOKEN)


def _size(result: Any) -> int:
    return len(json.dumps(result, ensure_ascii=False, default=str))


def _lists(value: Any, path: tuple = ()) -> list[tuple[tuple, list]]:
    """Every list inside a result, with where it is."""
    out = []
    if isinstance(value, list):
        out.append((path, value))
        for i, item in enumerate(value):
            out.extend(_lists(item, path + (i,)))
    elif isinstance(value, dict):
        for key, item in value.items():
            out.extend(_lists(item, path + (key,)))
    return out


def _strings(value: Any, path: tuple = ()) -> list[tuple[tuple, str]]:
    out = []
    if isinstance(value, str):
        out.append((path, value))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            out.extend(_strings(item, path + (i,)))
    elif isinstance(value, dict):
        for key, item in value.items():
            out.extend(_strings(item, path + (key,)))
    return out


def _set(root: Any, path: tuple, value: Any) -> None:
    node = root
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value


def trim_result(result: dict, max_chars: int) -> dict:
    """A copy of `result` of at most about `max_chars` characters of JSON, with
    `_trimmed` saying what was left out. Errors and small results come back as
    they are."""
    if _size(result) <= max_chars or not result.get("ok", True):
        return result
    out = json.loads(json.dumps(result, default=str))
    dropped: dict[str, list[int]] = {}
    # 1. Lists lose items from the end, the longest list first, down to one.
    while _size(out) > max_chars:
        lists = [(p, l) for p, l in _lists(out) if len(l) > 1]
        if not lists:
            break
        path, items = max(lists, key=lambda pl: _size(pl[1]))
        name = str(path[-1]) if path else "items"
        kept_total = dropped.setdefault(name, [len(items), len(items)])
        items.pop()
        kept_total[0] -= 1
    # 2. Long strings are shortened, the longest first.
    shortened = 0
    while _size(out) > max_chars:
        strings = [(p, s) for p, s in _strings(out) if len(s) > 200]
        if not strings:
            break
        path, text = max(strings, key=lambda ps: len(ps[1]))
        excess = _size(out) - max_chars
        keep = max(200, len(text) - excess - 40)
        _set(out, path, text[:keep].rstrip() + " [shortened]")
        shortened += 1
        if keep == 200:
            continue
    notes = [f"kept {kept} of {total} {name}" for name, (kept, total) in dropped.items()]
    if shortened:
        notes.append(f"{shortened} long text field(s) shortened")
    out["_trimmed"] = ("To keep the request within the model's size limit: " + "; ".join(notes)
                       + ". Search more narrowly to see what was left out.")
    return out


def fit_to_budget(messages: list[dict], tools: list[dict], budget: int,
                  tool_results: dict[str, tuple[str, dict]],
                  wrap: Callable[[str, dict], str]) -> tuple[list[dict], dict]:
    """The messages to send, with the largest tool results trimmed until the
    estimated request is within `budget` tokens, and a report.

    `tool_results` maps a tool message's tool_call_id to (tool name, compact
    result); `wrap` turns them back into the message content. Messages without
    an entry are never changed. `messages` itself is not modified.
    """
    before = request_tokens(messages, tools)
    report = {"estimated_tokens": before, "budget": budget, "trimmed": []}
    if budget <= 0 or before <= budget:
        return messages, report
    out = [dict(m) for m in messages]
    current = {m["tool_call_id"]: tool_results[m["tool_call_id"]][1]
               for m in out if m.get("role") == "tool" and m.get("tool_call_id") in tool_results}
    total = before
    while total > budget:
        candidates = [(cid, r) for cid, r in current.items()
                      if estimate_tokens(r) > MIN_RESULT_TOKENS and not r.get("_trimmed_floor")]
        if not candidates:
            break
        cid, result = max(candidates, key=lambda cr: estimate_tokens(cr[1]))
        size = estimate_tokens(result)
        target = max(MIN_RESULT_TOKENS, size - (total - budget) - 20)
        trimmed = trim_result(tool_results[cid][1], int(target * CHARS_PER_TOKEN))
        if estimate_tokens(trimmed) >= size:          # cannot shrink further
            trimmed = {**trimmed, "_trimmed_floor": True}
        current[cid] = trimmed
        for m in out:
            if m.get("tool_call_id") == cid and m.get("role") == "tool":
                m["content"] = wrap(tool_results[cid][0], {k: v for k, v in trimmed.items()
                                                          if k != "_trimmed_floor"})
        total = request_tokens(out, tools)
    report["estimated_tokens"] = total
    report["trimmed"] = sorted({cid for cid, r in current.items() if "_trimmed" in r})
    report["over_budget"] = total > budget
    return out, report
