"""Run the question set through the real /ask endpoint and score the answers.

    python -m eval.run_eval                      # every case
    python -m eval.run_eval --limit 5            # a smoke run
    python -m eval.run_eval --only nvda-revenue-fy2026
    python -m eval.run_eval --report out.json    # keep the per-case detail

Goes over HTTP to a running API rather than calling `run_agent` in-process, so
what is measured is the whole path a user takes: auth, conversation storage,
history loading, the loop, the tools. A harness that skips the endpoint measures
a component and reports it as a system.

One conversation per question
-----------------------------
Each case gets a fresh conversation. Sharing one would feed every answer into
the next question's history window, so case 12 would be scored on a system that
had already been told the answers to cases 1-11 -- which is a different system
from the one a user meets.

What the report separates, and why
----------------------------------
`wrong` is the system giving a number that misses. `no_number` is the extractor
finding nothing to score. Reporting them as one figure would either flatter the
system (counting unparseable answers as near-misses) or libel it (counting
correct prose as wrong), so they stay apart, and the per-case detail names which
answers fell into each.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

import httpx

from eval.datasets import Case, load_cases
from eval.metrics import Outcome, Summary, score_case, summarise

DEFAULT_API = "http://127.0.0.1:8000"
PASSWORD = "eval-harness-correct-horse"


class Session:
    """A throwaway account, and a token kept alive for the length of the run.

    Tokens expire after `jwt_expire_minutes` -- 30 by default. A 40-question
    run takes closer to 100 minutes on this host, so authenticating once and
    holding the token means every case after minute 30 fails with a 401. That
    is exactly what happened on the first full run: cases 16-40 errored at
    0.0s and the report showed 12.5% accuracy over 15 questions that had
    actually run. A harness that cannot outlive its own credentials measures
    its credentials.
    """

    def __init__(self, client: httpx.Client):
        self.client = client
        self.email = f"eval-{uuid.uuid4().hex[:12]}@example.com"
        response = client.post(
            "/auth/signup", json={"email": self.email, "password": PASSWORD}
        )
        response.raise_for_status()
        self._adopt(response.json()["access_token"])

    def _adopt(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}

    def refresh(self) -> None:
        response = self.client.post(
            "/auth/login", json={"email": self.email, "password": PASSWORD}
        )
        response.raise_for_status()
        self._adopt(response.json()["access_token"])

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send, and on a 401 log in again and send once more.

        Retried once, not in a loop: a second 401 means the credentials are
        wrong rather than stale, and retrying that forever would turn a
        configuration error into a hang.
        """
        response = self.client.request(method, path, headers=self.headers, **kwargs)
        if response.status_code == 401:
            self.refresh()
            response = self.client.request(
                method, path, headers=self.headers, **kwargs
            )
        response.raise_for_status()
        return response


def ask(session: Session, question: str) -> dict:
    """One question in its own conversation. Raises for transport failures."""
    conversation_id = session.request(
        "POST", "/conversations", json={"title": None}
    ).json()["id"]

    return session.request("POST", "/ask", json={
        "conversation_id": conversation_id,
        "message": question,
        "include_trace": True,
    }).json()


def run_case(session: Session, case: Case) -> Outcome:
    started = time.perf_counter()
    try:
        body = ask(session, case.question)
    except Exception as exc:  # noqa: BLE001 - a failed request is a scored case
        elapsed = (time.perf_counter() - started) * 1000
        return score_case(
            case, "", latency_ms=elapsed, tool_calls=0, iterations=0,
            prompt_tokens=0, completion_tokens=0, completed=False,
            stop_reason="request_failed", error=f"{type(exc).__name__}: {exc}",
        )

    steps = body.get("steps") or []
    tool_calls = [s for s in steps if s["tool"] not in
                  ("final_answer", "ungrounded_answer", "tool_call_budget")]
    return score_case(
        case, body["answer"],
        # The server's own measurement, not the client's: it excludes the HTTP
        # round trip and the two database writes, which are not what is being
        # evaluated.
        latency_ms=body["total_ms"],
        tool_calls=len(tool_calls),
        iterations=body["iterations"],
        prompt_tokens=body.get("prompt_tokens", 0),
        completion_tokens=body.get("completion_tokens", 0),
        completed=body["completed"],
        stop_reason=body["stop_reason"],
    )


def render(outcomes: list[Outcome]) -> str:
    """The report."""
    lines: list[str] = []
    add = lines.append

    overall = summarise("OVERALL", outcomes)

    add("=" * 96)
    add("RESULTS")
    add("=" * 96)
    add(f"{'':4}{'case':34} {'verdict':10} {'expected':>13} {'got':>13}  {'lat':>7} {'tools':>5}")
    add("-" * 96)
    for index, outcome in enumerate(outcomes, 1):
        got = "-" if outcome.extracted is None else f"{outcome.extracted:,.2f}"
        mark = {"pass": "PASS", "wrong": "WRONG", "no_number": "NO NUM",
                "error": "ERROR"}[outcome.verdict]
        add(f"{index:>3} {outcome.case_id:34} {mark:10} "
            f"{outcome.expected:>13,.2f} {got:>13}  "
            f"{outcome.latency_ms / 1000:>6.1f}s {outcome.tool_calls:>5}")

    add("")
    add("=" * 96)
    add("ACCURACY")
    add("=" * 96)
    add(f"  overall: {overall.passed}/{overall.total} = {overall.accuracy:.1f}%")
    add(f"    passed     {overall.passed:>3}")
    add(f"    wrong      {overall.wrong:>3}   (a number, outside tolerance)")
    add(f"    no number  {overall.no_number:>3}   (nothing the extractor could score)")
    add(f"    errors     {overall.errors:>3}   (the request itself failed)")

    add("")
    add("  by answerable_via:")
    groups: dict[str, list[Outcome]] = {}
    for outcome in outcomes:
        groups.setdefault(" + ".join(outcome.answerable_via), []).append(outcome)
    add(f"    {'category':34} {'n':>3} {'pass':>5} {'acc':>7} {'lat':>8} "
        f"{'tools':>6} {'tokens':>9}")
    add("    " + "-" * 78)
    for label in sorted(groups, key=lambda k: -len(groups[k])):
        summary = summarise(label, groups[label])
        add(f"    {label:34} {summary.total:>3} {summary.passed:>5} "
            f"{summary.accuracy:>6.1f}% {summary.mean_latency_ms / 1000:>7.1f}s "
            f"{summary.mean_tool_calls:>6.2f} {summary.total_tokens:>9,}")

    add("")
    add("  by company:")
    by_company: dict[str, list[Outcome]] = {}
    for outcome in outcomes:
        by_company.setdefault(outcome.company, []).append(outcome)
    for label in sorted(by_company):
        summary = summarise(label, by_company[label])
        add(f"    {label:12} {summary.passed:>3}/{summary.total:<3} "
            f"{summary.accuracy:>6.1f}%")

    latencies = [o.latency_ms for o in outcomes if not o.error]
    add("")
    add("=" * 96)
    add("COST AND LATENCY")
    add("=" * 96)
    if latencies:
        add(f"  latency   mean {statistics.mean(latencies) / 1000:>7.1f}s   "
            f"median {statistics.median(latencies) / 1000:>7.1f}s   "
            f"min {min(latencies) / 1000:>6.1f}s   max {max(latencies) / 1000:>7.1f}s")
        add(f"  total wall time for the run: "
            f"{sum(latencies) / 1000 / 60:.1f} min of model and tool time")
    add(f"  tool calls per question: mean {overall.mean_tool_calls:.2f}   "
        f"iterations per question: mean {overall.mean_iterations:.2f}")

    measured = [o for o in outcomes if o.total_tokens > 0]
    if measured:
        add(f"  tokens    prompt {overall.prompt_tokens:>10,}   "
            f"completion {overall.completion_tokens:>8,}   "
            f"total {overall.total_tokens:>10,}")
        add(f"            mean per question: "
            f"{overall.total_tokens / len(outcomes):,.0f}")
        if len(measured) != len(outcomes):
            add(f"            NOTE: usage reported for {len(measured)}/"
                f"{len(outcomes)} cases; the rest are excluded, not zero")
    else:
        add("  tokens    not reported by the upstream for any case")

    failures = [o for o in outcomes if o.verdict != "pass"]
    if failures:
        add("")
        add("=" * 96)
        add(f"FAILURES ({len(failures)})")
        add("=" * 96)
        for outcome in failures:
            add(f"  {outcome.case_id}  [{outcome.verdict}]")
            add(f"    asked    : {outcome.question}")
            add(f"    expected : {outcome.expected:,} {outcome.unit} "
                f"(+/- {outcome.tolerance})")
            got = "nothing extractable" if outcome.extracted is None \
                else f"{outcome.extracted:,}"
            add(f"    extracted: {got}")
            if outcome.error:
                add(f"    error    : {outcome.error}")
            answer = " ".join(outcome.answer.split())
            add(f"    answer   : {answer[:300]}")
            add("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--limit", type=int, default=None,
                        help="run only the first N cases")
    parser.add_argument("--only", action="append", default=None,
                        help="run only these case ids (repeatable)")
    parser.add_argument("--report", type=Path, default=None,
                        help="write per-case JSON here")
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args(argv)

    cases = list(load_cases())
    if args.only:
        wanted = set(args.only)
        cases = [c for c in cases if c.id in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    client = httpx.Client(base_url=args.api, timeout=args.timeout)
    try:
        client.get("/health").raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"API not reachable at {args.api}: {exc}", file=sys.stderr)
        return 2

    session = Session(client)
    print(f"running {len(cases)} cases against {args.api}", flush=True)

    outcomes: list[Outcome] = []
    started = time.perf_counter()
    for index, case in enumerate(cases, 1):
        outcome = run_case(session, case)
        outcomes.append(outcome)
        elapsed = time.perf_counter() - started
        print(f"  [{index:>2}/{len(cases)}] {outcome.verdict:9} {case.id:34} "
              f"{outcome.latency_ms / 1000:>6.1f}s   "
              f"(elapsed {elapsed / 60:.1f} min)", flush=True)
    client.close()

    report = render(outcomes)
    print()
    print(report)

    if args.report:
        args.report.write_text(json.dumps({
            "api": args.api,
            "cases": len(outcomes),
            "outcomes": [
                {
                    "case_id": o.case_id, "verdict": o.verdict,
                    "expected": o.expected, "extracted": o.extracted,
                    "unit": o.unit, "tolerance": o.tolerance,
                    "answerable_via": list(o.answerable_via), "company": o.company,
                    "latency_ms": o.latency_ms, "tool_calls": o.tool_calls,
                    "iterations": o.iterations, "prompt_tokens": o.prompt_tokens,
                    "completion_tokens": o.completion_tokens,
                    "completed": o.completed, "stop_reason": o.stop_reason,
                    "error": o.error, "answer": o.answer,
                }
                for o in outcomes
            ],
        }, indent=2), encoding="utf-8")
        print(f"\nper-case detail written to {args.report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
