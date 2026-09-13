"""Does the router itself cost throughput -- and if so, where?

    python benchmarks/router_overhead.py synthetic     # router alone, fake upstream, ~3 min
    python benchmarks/router_overhead.py ollama        # direct vs router on real Ollama, ~20 min
    python benchmarks/router_overhead.py summarize FILE

replica_ab.py compares "1 replica, direct" with "1 replica, via router", but it
records aggregates, and an aggregate gap cannot say *why*. The candidate causes
leave different fingerprints, so this measures the fingerprints:

  extra hop latency       TTFT through the router minus TTFT direct, measured
                          against a fake upstream where nothing else varies.
  relay cost per chunk    router CPU seconds per relayed token, same setup.
  CPU contention          the router is a Python process on the same saturated
                          cores as llama-server. If it steals time, generation
                          itself slows: lower decode tok/s *within* each stream.
  handoff gaps            Ollama with NUM_PARALLEL=1 serves one request at a
                          time. If the relay delayed a stream's end, the next
                          request would start later: wall time outside streaming,
                          wall - sum(total - ttft), grows while decode does not.
  connection pooling      ruled out by configuration rather than measured: the
                          router's pool is unbounded and the harness allows
                          concurrency + 10 connections.

Direct and router runs alternate (ABBA...), so drift as the laptop heats books
evenly to both.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib.util
import json
import pathlib
import statistics
import subprocess
import sys
import time

import httpx
import psutil

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO / "benchmarks" / "results" / "replicas"

_spec = importlib.util.spec_from_file_location("load_test", REPO / "scripts" / "load_test.py")
load_test = importlib.util.module_from_spec(_spec)
sys.modules["load_test"] = load_test          # @dataclass looks its module up here
_spec.loader.exec_module(load_test)

OLLAMA, ROUTER, FAKE = 11434, 11400, 11499
MODEL = "qwen2.5:1.5b"


# ─────────────────────────────────────────────────────────────────────────────
# Fake upstream: an OpenAI-shaped SSE stream with no model behind it
# ─────────────────────────────────────────────────────────────────────────────

def serve_fake(port: int, tokens: int, interval: float) -> None:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat() -> StreamingResponse:
        async def frames():
            for i in range(tokens):
                await asyncio.sleep(interval)
                chunk = {"choices": [{"delta": {"content": f"tok{i} "}}]}
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        return StreamingResponse(frames(), media_type="text/event-stream")

    @app.get("/api/version")
    async def version() -> dict:
        return {"version": "fake"}

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


# ─────────────────────────────────────────────────────────────────────────────
# Processes and CPU accounting
# ─────────────────────────────────────────────────────────────────────────────

def spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(args, cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def wait_for(url: str, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=1.0)
            return
        except httpx.RequestError:
            time.sleep(0.25)
    raise SystemExit(f"{url} did not come up")


def stop(proc: subprocess.Popen) -> None:
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    proc.wait(timeout=30)


def cpu_seconds(procs: list[psutil.Process]) -> float:
    total = 0.0
    for p in procs:
        try:
            t = p.cpu_times()
            total += t.user + t.system
        except psutil.Error:
            pass
    return total


def llama_servers() -> list[psutil.Process]:
    return [p for p in psutil.process_iter(["name"])
            if (p.info["name"] or "").lower().startswith("llama-server")]


def tree(proc: subprocess.Popen) -> list[psutil.Process]:
    root = psutil.Process(proc.pid)
    return [root, *root.children(recursive=True)]


# ─────────────────────────────────────────────────────────────────────────────
# One batch, decomposed
# ─────────────────────────────────────────────────────────────────────────────

def batch(base: str, concurrency: int, total: int, model: str | None, max_tokens: int,
          watch: dict[str, list[psutil.Process]]) -> dict:
    before = {k: cpu_seconds(v) for k, v in watch.items()}
    harness_before = time.process_time()
    results, wall = asyncio.run(load_test.run_batch(
        base, concurrency, total, model, max_tokens, False, 600.0))
    harness_cpu = time.process_time() - harness_before
    cpu = {k: cpu_seconds(v) - before[k] for k, v in watch.items()}

    ok = [r for r in results if r.ok and r.ttft is not None]
    tokens = sum(r.tokens for r in ok)
    streaming = sum(r.total - r.ttft for r in ok)
    decode = [(r.tokens - 1) / (r.total - r.ttft) for r in ok
              if r.tokens > 1 and r.total > r.ttft]
    return {
        "concurrency": concurrency, "requests": total, "ok": len(ok), "wall": wall,
        "tokens": tokens, "system_tps": tokens / wall,
        "decode_tps_p50": statistics.median(decode) if decode else None,
        "ttft_p50": statistics.median(r.ttft for r in ok) if ok else None,
        "total_mean": statistics.mean(r.total for r in ok) if ok else None,
        # Time the server was not streaming to anyone: prefill plus any gap
        # between one request ending and the next one's first token.
        "outside_streaming_s": wall - streaming,
        "cpu_s": cpu, "harness_cpu_s": harness_cpu,
    }


def alternate(rounds: int) -> list[str]:
    """ABBA ABBA ... so a linear drift lands equally on both paths."""
    order = []
    for i in range(rounds):
        order += ["direct", "router"] if i % 2 == 0 else ["router", "direct"]
    return order


def run_synthetic(args) -> pathlib.Path:
    path = OUT / f"router-overhead-synthetic-{dt.datetime.now():%Y%m%d-%H%M%S}.jsonl"
    for interval in (0.05, 0.0):          # a CPU model's pace (20 tok/s), then flat out
        fake = spawn([sys.executable, __file__, "_fake", "--port", str(FAKE),
                      "--tokens", str(args.max_tokens), "--interval", str(interval)])
        router = spawn([sys.executable, "-m", "inference_router", "--port", str(ROUTER),
                        "--upstream", f"http://127.0.0.1:{FAKE}"])
        try:
            wait_for(f"http://127.0.0.1:{FAKE}/api/version")
            wait_for(f"http://127.0.0.1:{ROUTER}/router/stats")
            watch = {"router": tree(router), "upstream": tree(fake)}
            for concurrency in (1, 8):
                for i, which in enumerate(alternate(args.rounds)):
                    port = ROUTER if which == "router" else FAKE
                    rec = batch(f"http://127.0.0.1:{port}", concurrency, 16, None,
                                args.max_tokens, watch)
                    rec.update(mode="synthetic", interval=interval, path=which, rep=i)
                    _append(path, rec)
                    _progress(rec)
        finally:
            stop(router)
            stop(fake)
    return path


def run_ollama(args) -> pathlib.Path:
    path = OUT / f"router-overhead-ollama-{dt.datetime.now():%Y%m%d-%H%M%S}.jsonl"
    httpx.post(f"http://127.0.0.1:{OLLAMA}/api/generate", timeout=300, json={
        "model": MODEL, "prompt": "hi", "stream": False, "options": {"num_predict": 1},
        "keep_alive": "60m"}).raise_for_status()
    router = spawn([sys.executable, "-m", "inference_router", "--port", str(ROUTER),
                    "--upstream", f"http://127.0.0.1:{OLLAMA}"])
    try:
        wait_for(f"http://127.0.0.1:{ROUTER}/router/stats")
        watch = {"router": tree(router), "llama_server": llama_servers()}
        for i, which in enumerate(alternate(args.rounds)):
            port = ROUTER if which == "router" else OLLAMA
            for concurrency, total in ((1, 6), (8, 16)):
                rec = batch(f"http://127.0.0.1:{port}", concurrency, total, MODEL,
                            args.max_tokens, watch)
                rec.update(mode="ollama", path=which, rep=i)
                _append(path, rec)
                _progress(rec)
    finally:
        stop(router)
    return path


def _append(path: pathlib.Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _progress(rec: dict) -> None:
    extra = f" interval={rec['interval']}" if "interval" in rec else ""
    print(f"  {rec['mode']}{extra} {rec['path']:<6} c={rec['concurrency']:<2} "
          f"{rec['system_tps']:7.1f} tok/s  decode p50 {rec['decode_tps_p50'] or 0:7.1f}  "
          f"TTFT p50 {rec['ttft_p50'] or 0:6.3f}s  outside-streaming {rec['outside_streaming_s']:6.2f}s  "
          f"router cpu {rec['cpu_s'].get('router', 0):5.2f}s", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

def summarize(path: pathlib.Path) -> None:
    rows = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    keys = sorted({(r.get("interval"), r["concurrency"]) for r in rows},
                  key=lambda k: (-(k[0] or 0), k[1]))

    def stat(group: list[dict], field: str, scale: float = 1.0) -> str:
        vals = [r[field] * scale for r in group if r.get(field) is not None]
        if not vals:
            return "-"
        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        return f"{statistics.mean(vals):.3f} ±{sd:.3f}"

    print(f"{path.name}  (mean ± sd across alternations)")
    for interval, c in keys:
        label = f"c={c}" + (f", fake upstream every {interval * 1000:.0f}ms"
                            if interval is not None else "")
        print(f"\n{label}")
        print(f"  {'path':<7}{'n':>3}{'system tok/s':>22}{'decode tok/s p50':>22}"
              f"{'TTFT p50 s':>18}{'outside stream s':>20}{'router CPU µs/tok':>20}")
        # Outside-streaming time assumes the upstream serves one request at a
        # time, as Ollama does at NUM_PARALLEL=1. The fake upstream serves all
        # of them at once, so above c=1 the streams overlap and the figure goes
        # negative -- meaningless rather than small.
        serialized = interval is None or c == 1
        for which in ("direct", "router"):
            g = [r for r in rows if r["concurrency"] == c and r.get("interval") == interval
                 and r["path"] == which]
            if not g:
                continue
            per_tok = [r["cpu_s"].get("router", 0) / r["tokens"] * 1e6 for r in g if r["tokens"]]
            router_cpu = (f"{statistics.mean(per_tok):.0f}" if which == "router" and per_tok
                          else "-")
            outside = stat(g, "outside_streaming_s") if serialized else "n/a"
            print(f"  {which:<7}{len(g):>3}{stat(g, 'system_tps'):>22}"
                  f"{stat(g, 'decode_tps_p50'):>22}{stat(g, 'ttft_p50'):>18}"
                  f"{outside:>20}{router_cpu:>20}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["synthetic", "ollama", "summarize", "_fake"])
    ap.add_argument("file", nargs="?", type=pathlib.Path)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--port", type=int, default=FAKE)
    ap.add_argument("--tokens", type=int, default=128)
    ap.add_argument("--interval", type=float, default=0.05)
    args = ap.parse_args()

    if args.mode == "_fake":
        serve_fake(args.port, args.tokens, args.interval)
        return 0
    if args.mode == "summarize":
        summarize(args.file)
        return 0
    path = run_synthetic(args) if args.mode == "synthetic" else run_ollama(args)
    print()
    summarize(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
