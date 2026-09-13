"""Does a second CPU replica buy throughput? The raw-inference half of the answer.

    python benchmarks/replica_ab.py                          # ~1 hour
    python benchmarks/replica_ab.py --summarize benchmarks/results/replicas/raw-XXXX.jsonl

Measures the inference servers alone -- no agent, no database -- with the same
request loop milestone 2 used (`scripts/load_test.py`'s run_batch), so these
numbers sit directly beside its finding that one Ollama gives 1.15x throughput
at 4x concurrency. The full-stack half is the Locust sweep with
INFERENCE_BASE_URL pointed at the router; docs/INFERENCE_REPLICAS.md has both.

Every configuration goes through inference_router except one, which measures the
router's own overhead by skipping it. The configurations exist to take apart the
obvious objections to a plain "two replicas vs one" number:

  - Two default replicas run 6 threads each: 12 threads on 10 physical cores.
    "3 threads each" holds the total thread budget equal to one replica's.
  - A second process is not the only way to serve two requests at once.
    "NUM_PARALLEL=2" batches them inside one process, with one copy of the
    weights -- the thing to beat, if replicas are to be worth their memory.

Two rounds, the second in reverse order, because a laptop's throughput drifts
as it heats up and a fixed order would book that drift to whichever
configuration ran last.

Prerequisites: the usual Ollama on 11434 with OLLAMA_NUM_PARALLEL=1 and
OLLAMA_CONTEXT_LENGTH=8192, `qwen2.5:1.5b` pulled, ports 11400 and 11435 free.
This script owns the second replica and the router, and restarts them between
configurations so no state carries over.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib.util
import json
import os
import pathlib
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO / "benchmarks" / "results" / "replicas"

_spec = importlib.util.spec_from_file_location("load_test", REPO / "scripts" / "load_test.py")
load_test = importlib.util.module_from_spec(_spec)
# Registered before executing: @dataclass looks its own module up in sys.modules.
sys.modules["load_test"] = load_test
_spec.loader.exec_module(load_test)

PRIMARY, SECOND, ROUTER = 11434, 11435, 11400
MODEL = "qwen2.5:1.5b"
MODEL_T3 = "qwen2.5:1.5b-t3"          # same weights, PARAMETER num_thread 3
# (concurrency, total requests). More requests than slots at every level, so
# throughput is measured over a queue that stays full rather than one burst.
LEVELS = [(1, 6), (2, 8), (4, 8), (8, 16)]
BASELINE = "router, 1 replica"


@dataclass(frozen=True)
class Config:
    name: str
    ports: tuple[int, ...]
    model: str = MODEL
    via_router: bool = True
    second_parallel: int = 1       # OLLAMA_NUM_PARALLEL for the replica on 11435


CONFIGS = [
    Config("direct, 1 replica", (PRIMARY,), via_router=False),
    Config(BASELINE, (PRIMARY,)),
    Config("router, 2 replicas", (PRIMARY, SECOND)),
    Config("router, 2 replicas, 3 threads each", (PRIMARY, SECOND), model=MODEL_T3),
    Config("router, 1 replica, 3 threads", (PRIMARY,), model=MODEL_T3),
    Config("router, 1 replica, NUM_PARALLEL=2", (SECOND,), second_parallel=2),
]


# ─────────────────────────────────────────────────────────────────────────────
# Processes
# ─────────────────────────────────────────────────────────────────────────────

def ollama_binary(explicit: str | None) -> str:
    candidates = [explicit, shutil.which("ollama"),
                  os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")]
    for c in candidates:
        if c and pathlib.Path(c).exists():
            return c
    raise SystemExit("ollama not found; pass --ollama PATH")


def port_answers(port: int) -> bool:
    try:
        httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)
        return True
    except httpx.RequestError:
        return False


def wait_for(url: str, seconds: float = 60.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return
        except httpx.RequestError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"{url} did not come up within {seconds:.0f}s")


def stop(proc: subprocess.Popen | None) -> None:
    """Kill the whole tree. `ollama serve` runs each model in a llama-server
    child, and killing only the parent orphans a process holding the weights."""
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.terminate()
    proc.wait(timeout=30)


def start_second_replica(binary: str, parallel: int, log: pathlib.Path) -> subprocess.Popen:
    env = {**os.environ,
           "OLLAMA_HOST": f"127.0.0.1:{SECOND}",
           "OLLAMA_CONTEXT_LENGTH": "8192",
           "OLLAMA_NUM_PARALLEL": str(parallel),
           "OLLAMA_MAX_LOADED_MODELS": "2"}
    proc = subprocess.Popen([binary, "serve"], env=env, stdout=log.open("ab"),
                            stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    wait_for(f"http://127.0.0.1:{SECOND}/api/version")
    return proc


def start_router(ports: tuple[int, ...], log: pathlib.Path) -> subprocess.Popen:
    args = [sys.executable, "-m", "inference_router", "--port", str(ROUTER)]
    for p in ports:
        args += ["--upstream", f"http://127.0.0.1:{p}"]
    proc = subprocess.Popen(args, cwd=REPO, stdout=log.open("ab"), stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    wait_for(f"http://127.0.0.1:{ROUTER}/router/stats")
    return proc


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────

def ensure_thread_variant() -> None:
    names = {m["name"] for m in httpx.get(f"http://127.0.0.1:{PRIMARY}/api/tags").json()["models"]}
    if MODEL_T3 in names or f"{MODEL_T3}:latest" in names:
        return
    httpx.post(f"http://127.0.0.1:{PRIMARY}/api/create", timeout=120, json={
        "model": MODEL_T3, "from": MODEL, "parameters": {"num_thread": 3}, "stream": False,
    }).raise_for_status()


def unload_everything(ports: tuple[int, ...]) -> None:
    """Nothing loaded anywhere before a configuration starts. Each replica holds
    a private copy of the weights (Ollama disables mmap on CPU), so a leftover
    model is memory the next configuration may need."""
    for port in ports:
        base = f"http://127.0.0.1:{port}"
        for m in httpx.get(f"{base}/api/ps").json().get("models", []):
            httpx.post(f"{base}/api/generate", json={"model": m["name"], "keep_alive": 0})
        deadline = time.monotonic() + 60
        while httpx.get(f"{base}/api/ps").json().get("models") and time.monotonic() < deadline:
            time.sleep(0.5)


def warm(port: int, model: str) -> None:
    """Load the model on this replica directly. Warming through the router would
    load it on one replica only, and the other would pay the load inside the
    first measured request."""
    httpx.post(f"http://127.0.0.1:{port}/api/generate", timeout=300, json={
        "model": model, "prompt": "hi", "stream": False,
        "options": {"num_predict": 1}, "keep_alive": "30m",
    }).raise_for_status()


# ─────────────────────────────────────────────────────────────────────────────
# Measurement
# ─────────────────────────────────────────────────────────────────────────────

def measure(base_url: str, model: str, concurrency: int, total: int, max_tokens: int) -> dict:
    results, wall = asyncio.run(load_test.run_batch(
        base_url, concurrency, total, model, max_tokens, False, 600.0))
    ok = [r for r in results if r.ok]
    tokens = sum(r.tokens for r in ok)
    med = lambda xs: statistics.median(xs) if xs else None
    return {
        "concurrency": concurrency, "requests": total, "ok": len(ok),
        "failed": total - len(ok), "wall": wall, "tokens": tokens,
        "system_tps": tokens / wall if wall else 0.0,
        "per_stream_tps": statistics.mean(r.tokens / r.total for r in ok) if ok else None,
        "ttft_p50": med([r.ttft for r in ok if r.ttft is not None]),
        "total_p50": med([r.total for r in ok]),
        "errors": [r.error for r in results if not r.ok][:3],
    }


def run(args) -> pathlib.Path:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUT / f"raw-{stamp}.jsonl"
    logs = OUT / f"logs-{stamp}"
    logs.mkdir()

    for port in (SECOND, ROUTER):
        if port_answers(port):
            raise SystemExit(f"port {port} is already in use; this script must own it")
    binary = ollama_binary(args.ollama)
    ensure_thread_variant()

    def write(record: dict) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    write({"type": "meta", "started": stamp, "levels": LEVELS, "max_tokens": args.max_tokens,
           "rounds": args.rounds, "cpu_logical": os.cpu_count(),
           "ollama": httpx.get(f"http://127.0.0.1:{PRIMARY}/api/version").json()["version"]})

    second_parallel = 1
    second = start_second_replica(binary, second_parallel, logs / "ollama-11435.log")
    router = None
    try:
        for rnd in range(1, args.rounds + 1):
            order = CONFIGS if rnd % 2 else list(reversed(CONFIGS))
            for cfg in order:
                print(f"\n[round {rnd}] {cfg.name}", flush=True)
                stop(router)
                router = None
                if cfg.second_parallel != second_parallel:
                    stop(second)
                    second_parallel = cfg.second_parallel
                    second = start_second_replica(binary, second_parallel,
                                                  logs / "ollama-11435.log")
                unload_everything((PRIMARY, SECOND))
                for port in cfg.ports:
                    warm(port, cfg.model)
                if cfg.via_router:
                    router = start_router(cfg.ports, logs / "router.log")
                    base = f"http://127.0.0.1:{ROUTER}"
                else:
                    base = f"http://127.0.0.1:{cfg.ports[0]}"

                for concurrency, total in LEVELS:
                    rec = measure(base, cfg.model, concurrency, total, args.max_tokens)
                    write({"type": "level", "round": rnd, "config": cfg.name, **rec})
                    print(f"  c={concurrency:<2} {rec['system_tps']:6.1f} tok/s system   "
                          f"TTFT p50 {rec['ttft_p50'] or 0:6.2f}s   ok {rec['ok']}/{total}",
                          flush=True)

                if router is not None:
                    stats = httpx.get(f"{base}/router/stats").json()
                    write({"type": "router_stats", "round": rnd, "config": cfg.name, **stats})
                time.sleep(args.cooldown)
    finally:
        stop(router)
        stop(second)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

def summarize(path: pathlib.Path) -> None:
    rows = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    meta = next(r for r in rows if r["type"] == "meta")
    levels = [r for r in rows if r["type"] == "level"]
    stats = [r for r in rows if r["type"] == "router_stats"]
    names = list(dict.fromkeys(r["config"] for r in levels))
    cs = sorted({r["concurrency"] for r in levels})

    def vals(name: str, c: int, key: str) -> list[float]:
        return [r[key] for r in levels
                if r["config"] == name and r["concurrency"] == c and r[key] is not None]

    def mean(name: str, c: int, key: str) -> float:
        v = vals(name, c, key)
        return statistics.mean(v) if v else float("nan")

    width = max(len(n) for n in names) + 2
    print(f"Raw inference, {MODEL}, {meta['max_tokens']} max tokens, "
          f"{meta['rounds']} rounds, Ollama {meta['ollama']}")
    print("\nSystem throughput, tok/s  (mean of rounds, [min-max])")
    print(f"{'':<{width}}" + "".join(f"{'c=' + str(c):>17}" for c in cs))
    for n in names:
        cells = []
        for c in cs:
            v = vals(n, c, "system_tps")
            cells.append(f"{statistics.mean(v):6.1f} [{min(v):4.1f}-{max(v):4.1f}]" if v else "-")
        print(f"{n:<{width}}" + "".join(f"{x:>17}" for x in cells))

    print(f"\nRelative to '{BASELINE}' at the same concurrency")
    print(f"{'':<{width}}" + "".join(f"{'c=' + str(c):>9}" for c in cs) + f"{'peak vs peak':>14}")
    base_peak = max(mean(BASELINE, c, "system_tps") for c in cs)
    for n in names:
        ratios = [mean(n, c, "system_tps") / mean(BASELINE, c, "system_tps") for c in cs]
        peak = max(mean(n, c, "system_tps") for c in cs)
        print(f"{n:<{width}}" + "".join(f"{r:>8.2f}x" for r in ratios) + f"{peak / base_peak:>13.2f}x")

    print("\nMedian time to first token, s  /  per-stream tok/s")
    print(f"{'':<{width}}" + "".join(f"{'c=' + str(c):>15}" for c in cs))
    for n in names:
        print(f"{n:<{width}}" + "".join(
            f"{mean(n, c, 'ttft_p50'):6.2f} / {mean(n, c, 'per_stream_tps'):5.1f}" .rjust(15)
            for c in cs))

    failed = sum(r["failed"] for r in levels)
    print(f"\nFailed requests across all runs: {failed}")
    if stats:
        print("\nRouter accounting (per round)")
        for s in stats:
            split = " / ".join(str(u["requests"]) for u in s["upstreams"])
            print(f"  round {s['round']}  {s['config']:<{width}} requests per replica {split:<9} "
                  f"busy_while_idle {s['busy_while_idle']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summarize", type=pathlib.Path, help="print the tables for a finished run")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--cooldown", type=float, default=30.0, help="seconds between configurations")
    ap.add_argument("--ollama", help="path to the ollama binary")
    args = ap.parse_args()

    path = args.summarize or run(args)
    print()
    summarize(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
