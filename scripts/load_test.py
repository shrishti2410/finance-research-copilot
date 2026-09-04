"""Concurrent load test against the inference proxy.

Fires N streaming chat requests at once and reports, per request:

    TTFT   time to first token -- queue wait + prefill. What the user "feels".
    TOTAL  full wall-clock time until the stream closes.
    TPOT   mean time per output token after the first -- the decode rate.

Plus a timeline showing which requests actually overlapped, which is the
clearest way to see whether the server is batching or just serializing.

    python scripts/load_test.py                      # 10 concurrent (default)
    python scripts/load_test.py -c 32 -n 128         # 32 in flight, 128 total
    python scripts/load_test.py --sweep              # 1,2,4,8,10 -- batching curve
    python scripts/load_test.py --same-prompt        # deliberately hit prefix cache

Point it at the proxy (default :8000) or straight at vLLM (:8001) to measure
the proxy's own overhead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from dataclasses import dataclass, field

import httpx

# Distinct prompts by default. Identical prompts would all hit vLLM's prefix
# cache and report a throughput number you will never see in production.
PROMPTS = [
    "Explain what a 10-K filing contains, in three sentences.",
    "What is the difference between gross margin and operating margin?",
    "Summarize why companies file an 8-K.",
    "Define free cash flow and why analysts care about it.",
    "What does a rising days-sales-outstanding suggest about a business?",
    "Explain EV/EBITDA and one situation where it misleads.",
    "What is the purpose of the MD&A section of an annual report?",
    "Describe how share buybacks affect earnings per share.",
    "What are the main risks disclosed in Item 1A of a 10-K?",
    "Explain the difference between GAAP and non-GAAP earnings.",
]


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Result:
    idx: int
    ok: bool = False
    started_at: float = 0.0          # offset from batch start
    ttft: float | None = None
    total: float = 0.0
    tokens: int = 0
    status: int | None = None
    error: str | None = None
    text: str = field(default="", repr=False)

    @property
    def tpot(self) -> float | None:
        """Seconds per output token after the first."""
        if self.ttft is None or self.tokens < 2:
            return None
        return (self.total - self.ttft) / (self.tokens - 1)


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def fmt(x: float | None, width: int = 7, unit: str = "s") -> str:
    return f"{'-':>{width}}" if x is None else f"{x:>{width}.3f}{unit}"


# ─────────────────────────────────────────────────────────────────────────────
# One request
# ─────────────────────────────────────────────────────────────────────────────

async def one_request(
    client: httpx.AsyncClient,
    idx: int,
    prompt: str,
    model: str | None,
    max_tokens: int,
    gate: asyncio.Event,
    batch_t0: float,
) -> Result:
    """Stream one chat completion, timing first token and completion."""
    r = Result(idx=idx)

    payload: dict = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
    }
    if model:
        payload["model"] = model

    # Every coroutine blocks here, so connection setup and JSON encoding don't
    # stagger the actual start times. Release is as close to simultaneous as
    # asyncio allows.
    await gate.wait()

    t0 = time.perf_counter()
    r.started_at = t0 - batch_t0
    parts: list[str] = []

    try:
        async with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
            r.status = resp.status_code
            if resp.status_code != 200:
                body = await resp.aread()
                r.error = f"HTTP {resp.status_code}: {body.decode('utf-8', 'replace')[:160]}"
                r.total = time.perf_counter() - t0
                return r

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                piece = (choices[0].get("delta") or {}).get("content")
                if not piece:
                    continue

                if r.ttft is None:
                    r.ttft = time.perf_counter() - t0
                r.tokens += 1
                parts.append(piece)

        r.total = time.perf_counter() - t0
        r.text = "".join(parts)
        r.ok = r.tokens > 0
        if not r.ok:
            r.error = "stream closed with no content"

    except httpx.RequestError as exc:
        r.total = time.perf_counter() - t0
        r.error = f"{type(exc).__name__}: {exc}"

    return r


# ─────────────────────────────────────────────────────────────────────────────
# A batch
# ─────────────────────────────────────────────────────────────────────────────

async def run_batch(
    base_url: str,
    concurrency: int,
    total_requests: int,
    model: str | None,
    max_tokens: int,
    same_prompt: bool,
    timeout: float,
) -> tuple[list[Result], float]:
    """Run `total_requests` with at most `concurrency` in flight."""
    sem = asyncio.Semaphore(concurrency)
    gate = asyncio.Event()

    # max_connections must exceed concurrency or httpx queues requests in the
    # pool -- which looks exactly like server-side queueing and would silently
    # corrupt the measurement.
    limits = httpx.Limits(max_connections=concurrency + 10, max_keepalive_connections=concurrency + 10)

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(connect=10, read=timeout, write=30, pool=timeout),
        limits=limits,
    ) as client:
        batch_t0 = time.perf_counter()

        async def guarded(i: int) -> Result:
            async with sem:
                prompt = PROMPTS[0] if same_prompt else PROMPTS[i % len(PROMPTS)]
                if not same_prompt:
                    # Keep prompts unique even past len(PROMPTS).
                    prompt = f"({i + 1}) {prompt}"
                return await one_request(client, i, prompt, model, max_tokens, gate, batch_t0)

        tasks = [asyncio.create_task(guarded(i)) for i in range(total_requests)]
        await asyncio.sleep(0.05)   # let every task reach the gate
        batch_t0 = time.perf_counter()
        gate.set()

        results = await asyncio.gather(*tasks)
        wall = time.perf_counter() - batch_t0

    return list(results), wall


async def warmup(base_url: str, model: str | None, timeout: float) -> bool:
    """One throwaway request. The first call pays for model load, CUDA graph
    capture and cold caches; folding that into request #1 skews everything."""
    payload: dict = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8, "temperature": 0}
    if model:
        payload["model"] = model
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as c:
            resp = await c.post("/v1/chat/completions", json=payload)
            return resp.status_code == 200
    except httpx.RequestError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

def print_results(results: list[Result], wall: float, concurrency: int) -> None:
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]

    print(f"\n  {'#':>3}  {'start':>7}  {'TTFT':>8}  {'TOTAL':>8}  {'tok':>5}  {'TPOT':>8}  {'tok/s':>6}")
    print(f"  {'-'*3}  {'-'*7}  {'-'*8}  {'-'*8}  {'-'*5}  {'-'*8}  {'-'*6}")
    for r in results:
        if not r.ok:
            print(f"  {r.idx:>3}  {r.started_at:>6.3f}s  {'FAILED':>8}  {r.total:>7.3f}s  "
                  f"{'-':>5}  {'-':>8}  {'-':>6}   {r.error}")
            continue
        rate = r.tokens / r.total if r.total else 0
        tpot_ms = f"{r.tpot * 1000:.1f}ms" if r.tpot else "-"
        print(f"  {r.idx:>3}  {r.started_at:>6.3f}s  {r.ttft:>7.3f}s  {r.total:>7.3f}s  "
              f"{r.tokens:>5}  {tpot_ms:>8}  {rate:>6.1f}")

    if not ok:
        print(f"\n  All {len(results)} requests failed.")
        return

    ttfts = [r.ttft for r in ok if r.ttft is not None]
    totals = [r.total for r in ok]
    out_tokens = sum(r.tokens for r in ok)

    print(f"\n  {'':<12}{'mean':>9}{'p50':>9}{'p95':>9}{'min':>9}{'max':>9}")
    print(f"  {'-'*12}{'-'*9}{'-'*9}{'-'*9}{'-'*9}{'-'*9}")
    for label, vals in (("TTFT", ttfts), ("TOTAL", totals)):
        if vals:
            print(f"  {label:<12}{statistics.mean(vals):>8.3f}s{pct(vals,50):>8.3f}s"
                  f"{pct(vals,95):>8.3f}s{min(vals):>8.3f}s{max(vals):>8.3f}s")

    print(f"""
  concurrency          {concurrency}
  requests             {len(ok)} ok, {len(bad)} failed
  wall clock           {wall:.3f}s
  output tokens        {out_tokens}
  system throughput    {out_tokens / wall:.1f} tok/s   <- aggregate across all streams
  per-stream mean      {statistics.mean([r.tokens / r.total for r in ok]):.1f} tok/s""")


def print_timeline(results: list[Result], wall: float, width: int = 58) -> None:
    """ASCII gantt. Overlapping bars = real batching. A staircase = serialization."""
    ok = [r for r in results if r.ok]
    if not ok or wall <= 0:
        return

    # ASCII only: the Windows console is cp1252 and mangles box-drawing chars.
    print(f"\n  Timeline   '.' = waiting for first token   '#' = generating\n")
    for r in results:
        if not r.ok:
            print(f"  {r.idx:>3} | (failed)")
            continue
        lead = int(r.started_at / wall * width)
        ttft_w = max(1, int((r.ttft or 0) / wall * width))
        gen_w = max(1, int((r.total - (r.ttft or 0)) / wall * width))
        bar = " " * lead + "." * ttft_w + "#" * gen_w
        print(f"  {r.idx:>3} | {bar[:width]}")
    print(f"      +{'-' * width}")
    print(f"       0s{' ' * (width - 10)}{wall:.2f}s")


def print_interpretation() -> None:
    print("""
  Reading this
  ------------
  Continuous batching (vLLM) shows: bars overlap heavily, TTFT stays broadly
  flat as concurrency rises, per-stream tok/s drops somewhat, and system
  throughput climbs sharply. Decode is memory-bandwidth-bound, so extra
  sequences ride along on weight reads that were happening anyway -- nearly
  free until compute or KV-cache memory runs out.

  No batching (Ollama, llama.cpp default) shows: a staircase timeline, TTFT
  climbing roughly linearly with queue position, and system throughput flat no
  matter the concurrency. Requests are simply waiting their turn.

  If TTFT grows but the timeline still overlaps, the batch is being admitted
  but prefill is contending -- normal at high concurrency with long prompts.""")


# ─────────────────────────────────────────────────────────────────────────────

async def sweep(args, model: str | None) -> None:
    levels = [int(x) for x in args.sweep_levels.split(",")]
    print(f"\nSweep: concurrency {levels}  ({args.max_tokens} max_tokens each)")
    print("=" * 78)

    rows = []
    for c in levels:
        print(f"\n--- concurrency {c} " + "-" * 58)
        results, wall = await run_batch(
            args.base_url, c, c, model, args.max_tokens, args.same_prompt, args.timeout
        )
        ok = [r for r in results if r.ok]
        if not ok:
            print("  all failed")
            continue
        ttfts = [r.ttft for r in ok if r.ttft is not None]
        tok = sum(r.tokens for r in ok)
        rows.append((c, statistics.mean(ttfts), statistics.mean([r.total for r in ok]),
                     tok / wall, statistics.mean([r.tokens / r.total for r in ok])))
        print(f"  mean TTFT {statistics.mean(ttfts):.3f}s   "
              f"system {tok / wall:.1f} tok/s   per-stream {rows[-1][4]:.1f} tok/s")

    if not rows:
        return
    print("\n" + "=" * 78)
    print(f"  {'conc':>5}  {'mean TTFT':>10}  {'mean total':>11}  {'system tok/s':>13}  {'per-stream':>11}  scaling")
    print(f"  {'-'*5}  {'-'*10}  {'-'*11}  {'-'*13}  {'-'*11}  {'-'*8}")
    base_tp = rows[0][3]
    for c, ttft, total, sys_tp, per in rows:
        print(f"  {c:>5}  {ttft:>9.3f}s  {total:>10.3f}s  {sys_tp:>13.1f}  {per:>11.1f}  {sys_tp / base_tp:>6.2f}x")
    print("""
  'scaling' is system throughput relative to concurrency 1. Near-linear means
  batching is working. Flat at ~1.00x means requests are serializing.""")


async def main_async() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000", help="proxy (8000) or vLLM directly (8001)")
    ap.add_argument("-c", "--concurrency", type=int, default=10)
    ap.add_argument("-n", "--requests", type=int, default=None, help="total requests (default: = concurrency)")
    ap.add_argument("--model", default=None, help="override; proxy fills its default if omitted")
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--same-prompt", action="store_true", help="reuse one prompt (hits prefix cache)")
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--sweep", action="store_true", help="run several concurrency levels and compare")
    ap.add_argument("--sweep-levels", default="1,2,4,8,10")
    args = ap.parse_args()

    args.base_url = args.base_url.rstrip("/")
    total = args.requests or args.concurrency

    print(f"Target      {args.base_url}")
    print(f"Prompts     {'identical (prefix-cache hit)' if args.same_prompt else 'distinct'}")

    if not args.no_warmup:
        print("Warmup      ...", end="", flush=True)
        if await warmup(args.base_url, args.model, args.timeout):
            print(" ok")
        else:
            print(" FAILED -- server unreachable or not ready.")
            print("            Check: curl " + args.base_url + "/v1/health")
            return 1

    if args.sweep:
        await sweep(args, args.model)
        print_interpretation()
        return 0

    print(f"Running     {total} requests, {args.concurrency} concurrent, "
          f"max_tokens={args.max_tokens}")
    print("=" * 78)

    results, wall = await run_batch(
        args.base_url, args.concurrency, total, args.model,
        args.max_tokens, args.same_prompt, args.timeout,
    )

    print_results(results, wall, args.concurrency)
    print_timeline(results, wall)
    print_interpretation()

    return 0 if any(r.ok for r in results) else 1


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
