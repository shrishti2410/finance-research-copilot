"""Full stack, OLLAMA_NUM_PARALLEL=1 vs 2: does the raw-inference win survive the agent?

    python benchmarks/parallel_ab.py                     # ~95 min: 4 cells x 20 min
    python benchmarks/parallel_ab.py --summarize DIR     # tables for a finished run

replica_ab.py measured NUM_PARALLEL=2 at 1.29-1.34x on raw inference. That is not
the question that decides a default. This asks the one that is: does a user of
the full stack -- auth, agent loop, tools, database, two models -- get more
answers?

It runs the app's real inference setup, one Ollama serving qwen2.5:7b (answers)
and qwen2.5:1.5b (tool routing), through the corrected Locust harness in
run_sweep.sh: streaming timed by hand, guard refusals counted as delivered, and a
fresh account pool with fresh tokens for every cell.

Cells run ABBA -- (1, 2 users), (2, 2), (2, 5), (1, 5) -- so drift over the run
lands on both settings. Ollama is restarted for every cell with the setting
under test, and each runner's command line is checked for `-np N`: a setting
that silently failed to apply would turn the comparison into a comparison of
nothing.

Memory is sampled every 15 seconds. docs/DEPLOYMENT.md records free RAM dipping
to 0.08 GB on this host with both models loading at NUM_PARALLEL=1, and
NUM_PARALLEL=2 adds ~674 MB of KV cache on top. If Windows pages, the
NUM_PARALLEL=2 cells slow down for a reason that belongs to this laptop, and the
record has to show that rather than let it pass as a result.

Decision rule, fixed before the run: NUM_PARALLEL=2 counts as a real full-stack
improvement only if, at both 2 and 5 users, delivered answers per minute rise by
at least 15% AND median latency falls, with no more failures. One run per cell
has no variance of its own, so the bar is about half the raw-inference gain
rather than "any increase".

This takes over port 11434. It stops the Ollama tray app and its server, and
starts the tray app again when it finishes, so the machine is left as it was.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time

import httpx
import psutil

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
OUT_ROOT = HERE / "results" / "parallel"

_spec = importlib.util.spec_from_file_location("summarize_sweep", HERE / "summarize_sweep.py")
summarize_sweep = importlib.util.module_from_spec(_spec)
sys.modules["summarize_sweep"] = summarize_sweep
_spec.loader.exec_module(summarize_sweep)

PORT = 11434
MODELS = ("qwen2.5:7b", "qwen2.5:1.5b")      # AGENT_MODEL, AGENT_ROUTER_MODEL
CELLS = [(1, 2), (2, 2), (2, 5), (1, 5)]    # (NUM_PARALLEL, users), ABBA
MIN_GAIN = 1.15
# Sustained hard page-ins per second above which a cell measured disk reads, not
# NUM_PARALLEL. Idle on this host reads 30-80. The failure this guards against was
# observed: a 7b prompt batch that normally takes ~40s took 54 minutes with the
# weights paged out. 500 pages/s is ~2 MB/s of sustained reads from the pagefile.
PAGING_LIMIT = 500.0
OLLAMA_DIR = pathlib.Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama"))


# ─────────────────────────────────────────────────────────────────────────────
# Ollama on 11434
# ─────────────────────────────────────────────────────────────────────────────

def answers(url: str) -> bool:
    try:
        httpx.get(url, timeout=2.0)
        return True
    except httpx.RequestError:
        return False


def wait_until(predicate, what: str, seconds: float = 90.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise SystemExit(f"timed out waiting for {what}")


def stop_every_ollama() -> None:
    """Tray app, server, and the llama-server children holding the weights."""
    for proc in psutil.process_iter(["name"]):
        if (proc.info["name"] or "").lower() in ("ollama app.exe", "ollama.exe"):
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    wait_until(lambda: not answers(f"http://127.0.0.1:{PORT}/api/version")
               and not any((p.info["name"] or "").lower().startswith("llama-server")
                           for p in psutil.process_iter(["name"])),
               "Ollama to stop")


def start_server(parallel: int, log: pathlib.Path) -> subprocess.Popen:
    env = {**os.environ,
           "OLLAMA_HOST": f"127.0.0.1:{PORT}",
           "OLLAMA_CONTEXT_LENGTH": "8192",
           "OLLAMA_NUM_PARALLEL": str(parallel),
           "OLLAMA_MAX_LOADED_MODELS": "2",
           "OLLAMA_KEEP_ALIVE": "60m"}
    proc = subprocess.Popen([str(OLLAMA_DIR / "ollama.exe"), "serve"], env=env,
                            stdout=log.open("ab"), stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    wait_until(lambda: answers(f"http://127.0.0.1:{PORT}/api/version"), "Ollama to start")
    return proc


def warm_and_verify(parallel: int, log: pathlib.Path) -> list[str]:
    for model in MODELS:
        httpx.post(f"http://127.0.0.1:{PORT}/api/generate", timeout=600, json={
            "model": model, "prompt": "hi", "stream": False,
            "options": {"num_predict": 1}, "keep_alive": "60m",
        }).raise_for_status()
    launched = re.findall(r"(-c \d+ -np \d+)", log.read_text(encoding="utf-8", errors="replace"))
    recent = launched[-len(MODELS):]
    if len(recent) < len(MODELS) or any(f"-np {parallel}" not in r for r in recent):
        raise SystemExit(f"NUM_PARALLEL={parallel} did not apply; runners launched with {recent}")
    return recent


def restore_tray_app() -> None:
    if answers(f"http://127.0.0.1:{PORT}/api/version"):
        return
    subprocess.Popen([str(OLLAMA_DIR / "ollama app.exe")],
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    wait_until(lambda: answers(f"http://127.0.0.1:{PORT}/api/version"), "the tray app's Ollama")


# ─────────────────────────────────────────────────────────────────────────────
# Memory
# ─────────────────────────────────────────────────────────────────────────────

def pages_input_per_second() -> float | None:
    """System-wide hard page-ins per second, from the Windows counter.

    Free memory alone cannot show paging: the pagefile absorbs the shortfall and
    "available" can look steady while every token faults weights back in from
    disk. Two samples, because a rate counter's first reading has no interval.
    """
    try:
        out = subprocess.run(["typeperf", r"\Memory\Pages Input/sec", "-sc", "2"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = [line for line in out.splitlines()
            if line.startswith('"') and not line.startswith('"(PDH')]
    try:
        return float(rows[-1].rsplit(",", 1)[-1].strip('"'))
    except (IndexError, ValueError):
        return None


class MemorySampler(threading.Thread):
    def __init__(self, path: pathlib.Path, every: float = 15.0) -> None:
        super().__init__(daemon=True)
        self.path, self.every, self.done = path, every, threading.Event()

    def run(self) -> None:
        with self.path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["unix_time", "available_mb", "swap_used_mb", "pages_input_per_s"])
            while not self.done.is_set():
                paging = pages_input_per_second()
                w.writerow([round(time.time()), psutil.virtual_memory().available // 2**20,
                            psutil.swap_memory().used // 2**20,
                            "" if paging is None else round(paging, 1)])
                f.flush()
                self.done.wait(self.every)


# ─────────────────────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────────────────────

def run(args) -> pathlib.Path:
    bash = shutil.which("bash")
    if not bash:
        raise SystemExit("bash is required to drive run_sweep.sh")
    root = OUT_ROOT / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    root.mkdir(parents=True)
    meta = {"host": args.host, "duration": args.duration, "models": MODELS,
            "cells": [], "min_gain": MIN_GAIN}

    server = None
    try:
        for parallel, users in CELLS:
            cell = root / f"np{parallel}-u{users:02d}"
            cell.mkdir()
            print(f"\n=== NUM_PARALLEL={parallel}, {users} users ===", flush=True)

            stop_every_ollama()
            server = start_server(parallel, cell / "ollama.log")
            runners = warm_and_verify(parallel, cell / "ollama.log")
            health = httpx.get(f"{args.host}/health", timeout=30)
            if health.status_code != 200:
                raise SystemExit(f"API not healthy before the cell: {health.text[:300]}")

            sampler = MemorySampler(cell / "memory.csv")
            sampler.start()
            started = time.time()
            try:
                subprocess.run([bash, str(HERE / "run_sweep.sh")], check=False, env={
                    **os.environ, "HOST": args.host, "OUT": cell.as_posix(), "FRESH": "1",
                    "POOL_SIZE": "5", "LEVELS": str(users), "DURATION": args.duration,
                })
            finally:
                sampler.done.set()
                sampler.join()

            meta["cells"].append({
                "parallel": parallel, "users": users, "dir": cell.name,
                "started": started, "finished": time.time(), "runners": runners,
                "ollama": httpx.get(f"http://127.0.0.1:{PORT}/api/version").json()["version"],
            })
            (root / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    finally:
        if server is not None:
            subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"], capture_output=True)
        wait_until(lambda: not answers(f"http://127.0.0.1:{PORT}/api/version"), "Ollama to stop")
        restore_tray_app()
    return root


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

def summarize(root: pathlib.Path) -> None:
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    rows = {}
    for c in meta["cells"]:
        cell = root / c["dir"]
        d = summarize_sweep.read_level(cell / f"c{c['users']:02d}_stats.csv")
        mem = list(csv.DictReader((cell / "memory.csv").open(encoding="utf-8")))
        d["min_available_mb"] = min(int(m["available_mb"]) for m in mem)
        d["swap_growth_mb"] = int(mem[-1]["swap_used_mb"]) - int(mem[0]["swap_used_mb"])
        paging = [float(m["pages_input_per_s"]) for m in mem if m.get("pages_input_per_s")]
        d["paging_mean"] = sum(paging) / len(paging) if paging else float("nan")
        d["paging_max"] = max(paging) if paging else float("nan")
        d["answers"] = d["outcomes"].get("final_answer", 0)
        d["runners"] = c["runners"]
        # Each model load starts a llama-server. Two are the warm-up; any more
        # mean a model was evicted and reloaded mid-cell -- 172-207s each on this
        # CPU (docs/DEPLOYMENT.md), which would swamp the difference measured.
        launches = (cell / "ollama.log").read_text(encoding="utf-8", errors="replace") \
            .count("starting llama-server")
        d["reloads"] = launches - len(MODELS)
        rows[(c["parallel"], c["users"])] = d

    secs = lambda v: "-" if v != v else f"{v:.0f}s"
    print(f"Full stack, {meta['duration']} per cell, models {', '.join(meta['models'])}")
    print("=" * 112)
    print(f"{'NUM_PARALLEL':>12} {'users':>5} {'tried':>6} {'done':>5} {'failed':>7} {'answers':>8} "
          f"{'ok/min':>7} {'total med':>10} {'total p95':>10} {'TTFT med':>9} "
          f"{'min free':>9} {'swap +':>7} {'reloads':>8} {'page-ins/s mean/max':>20}")
    for (parallel, users), d in sorted(rows.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        print(f"{parallel:>12} {users:>5} {d['attempts']:>6} {d['requests']:>5} {d['failures']:>7} "
              f"{d['answers']:>8} {d['delivered_per_min']:>7.2f} {secs(d['total_med']):>10} "
              f"{secs(d['total_p95']):>10} {secs(d['ttft_med']):>9} "
              f"{d['min_available_mb']:>7} MB {d['swap_growth_mb']:>4} MB {d['reloads']:>8} "
              f"{d['paging_mean']:>10.0f} / {d['paging_max']:<7.0f}")

    print("\nOutcomes by stop_reason")
    for (parallel, users), d in sorted(rows.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        print(f"  NUM_PARALLEL={parallel}, {users} users: {d['outcomes']}   runners {d['runners']}")

    print(f"\nDecision rule: at every level, ok/min >= {MIN_GAIN:.2f}x AND lower median "
          f"latency AND no more failures")
    verdicts = []
    for users in sorted({u for _, u in rows}):
        one, two = rows.get((1, users)), rows.get((2, users))
        if not one or not two:
            print(f"  {users} users: incomplete")
            verdicts.append(False)
            continue
        gain = two["delivered_per_min"] / one["delivered_per_min"] if one["delivered_per_min"] else float("inf")
        latency = two["total_med"] / one["total_med"]
        ttft = two["ttft_med"] / one["ttft_med"]
        ok = gain >= MIN_GAIN and latency < 1.0 and two["failures"] <= one["failures"]
        verdicts.append(ok)
        print(f"  {users} users: ok/min {gain:.2f}x   median latency {latency:.2f}x   "
              f"TTFT {ttft:.2f}x   failures {one['failures']} -> {two['failures']}   "
              f"{'PASS' if ok else 'FAIL'}")
    reloaded = [k for k, d in rows.items() if d["reloads"] > 0]
    paged = [k for k, d in rows.items() if d["paging_mean"] > PAGING_LIMIT]
    if paged:
        print(f"\nVerdict: INCONCLUSIVE -- sustained paging (> {PAGING_LIMIT:.0f} page-ins/s "
              f"on average) in {sorted(paged)}; those cells measured the pagefile, not "
              f"NUM_PARALLEL. Free memory and re-run.")
    elif reloaded:
        print(f"\nVerdict: INCONCLUSIVE -- models reloaded mid-cell in {sorted(reloaded)}; "
              f"the comparison measured model loading, not NUM_PARALLEL. Free memory and re-run.")
    else:
        print(f"\nVerdict: {'NUM_PARALLEL=2 is a real full-stack improvement' if all(verdicts) else 'not shown -- keep NUM_PARALLEL=1'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summarize", type=pathlib.Path)
    ap.add_argument("--host", default="http://127.0.0.1:8000")
    ap.add_argument("--duration", default="20m")
    args = ap.parse_args()
    root = args.summarize or run(args)
    print()
    summarize(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
