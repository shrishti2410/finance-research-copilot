"""Turn the sweep's CSVs into the comparison the sweep was run to produce.

    python benchmarks/summarize_sweep.py

Reads benchmarks/results/c{NN}_stats.csv and reports, per concurrency level:
throughput, total latency, time to first token, and how the two diverge -- which
is what distinguishes a system that is working harder from one that is queueing.

Modelled on the interpretation section of `scripts/load_test.py`, because the
question is the same one M2 asked of the raw inference server, and the answer
should be readable the same way.
"""

from __future__ import annotations

import csv
import pathlib
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS = pathlib.Path(__file__).parent / "results"


def read_level(stats_csv: pathlib.Path) -> dict:
    rows = list(csv.DictReader(stats_csv.open(encoding="utf-8")))
    by_name = {r["Name"]: r for r in rows}

    def num(row: dict | None, key: str) -> float:
        if not row or not row.get(key):
            return float("nan")
        try:
            return float(row[key])
        except ValueError:
            return float("nan")

    total = by_name.get("ask/stream: TOTAL")
    ttft = by_name.get("ask/stream: first token")
    headers = by_name.get("POST /ask/stream (to headers only)")

    outcomes = {name.split("outcome: ", 1)[1]: int(row["Request Count"])
                for name, row in by_name.items() if name.startswith("outcome: ")}

    # A level where nothing finished has no TOTAL row at all. That is not a
    # missing measurement -- it is the measurement, and it must be reported as
    # zero completions rather than crashing the summary. The attempt count comes
    # from the headers row, which every request produces whether or not it ever
    # returns an answer.
    def count(row: dict | None, key: str) -> int:
        value = num(row, key)
        return 0 if value != value else int(value)      # NaN-safe

    return {
        "requests": count(total, "Request Count"),
        "failures": count(total, "Failure Count"),
        "attempts": count(headers, "Request Count"),
        "attempt_failures": count(headers, "Failure Count"),
        "total_mean": num(total, "Average Response Time") / 1000,
        "total_med": num(total, "Median Response Time") / 1000,
        "total_p95": num(total, "95%") / 1000,
        "total_max": num(total, "Max Response Time") / 1000,
        "ttft_mean": num(ttft, "Average Response Time") / 1000,
        "ttft_med": num(ttft, "Median Response Time") / 1000,
        "ttft_p95": num(ttft, "95%") / 1000,
        "headers_med": num(headers, "Median Response Time"),
        "rps": num(total, "Requests/s"),
        "outcomes": outcomes,
    }


def main() -> int:
    levels = []
    for path in sorted(RESULTS.glob("c*_stats.csv")):
        match = re.search(r"c(\d+)_stats\.csv$", path.name)
        if not match:
            continue
        levels.append((int(match.group(1)), read_level(path)))

    if not levels:
        print(f"No sweep results in {RESULTS}. Run benchmarks/run_sweep.sh first.")
        return 1

    print("Full stack under concurrent load: auth, agent loop, tools, DB, inference")
    print("=" * 78)
    print(f"{'users':>6} {'tried':>6} {'done':>5} {'failed':>7} {'req/min':>8} "
          f"{'total med':>10} {'total p95':>10} {'TTFT med':>9}")
    print("-" * 78)
    for users, d in levels:
        def s(v):
            return "     -" if v != v else f"{v:>5.0f}s"
        print(f"{users:>6} {d['attempts']:>6} {d['requests']:>5} "
              f"{d['attempt_failures']:>7} {d['rps'] * 60:>8.1f} "
              f"{s(d['total_med']):>10} {s(d['total_p95']):>10} {s(d['ttft_med']):>9}")

    base_users, base = levels[0]
    print(f"\nScaling, relative to {base_users} users")
    print("-" * 78)
    print(f"{'users':>6} {'throughput':>11} {'per-user tput':>14} "
          f"{'latency':>9} {'TTFT':>9}")
    for users, d in levels:
        if not d["requests"]:
            print(f"{users:>6} {'nothing completed':>26}")
            continue
        tput = d["rps"] / base["rps"] if base["rps"] else float("nan")
        per_user = tput / (users / base_users)
        print(f"{users:>6} {tput:>10.2f}x {per_user:>13.2f}x "
              f"{d['total_med'] / base['total_med']:>8.2f}x "
              f"{d['ttft_med'] / base['ttft_med']:>8.2f}x")

    print("\nOutcomes by stop_reason (not failures unless marked)")
    print("-" * 78)
    every = sorted({k for _, d in levels for k in d["outcomes"]})
    print(f"{'users':>6}  " + "  ".join(f"{k[:22]:>22}" for k in every))
    for users, d in levels:
        print(f"{users:>6}  " + "  ".join(
            f"{d['outcomes'].get(k, 0):>22}" for k in every))

    print(f"""
Reading this
------------
Throughput staying flat while latency rises in proportion to concurrency means
requests are queueing, not being served in parallel. That is the expected result
here: `scripts/load_test.py --sweep` measured the inference server at 1.15x
system throughput for 4x concurrency, so there is almost no batching to exploit,
and the agent loop cannot be faster than the model it waits on.

TTFT rising as fast as total latency locates the wait before generation -- the
request is holding a slot in a queue. If TTFT stayed flat while total grew, the
opposite would be true: generation itself would be slowing under contention.

`inference_error` appearing only at higher concurrency is the 300s read timeout
firing on a request that waited too long for a slot. It is the first hard
failure this stack produces under load, and the number of users at which it
starts is the real capacity limit of the deployment.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
