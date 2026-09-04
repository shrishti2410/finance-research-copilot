"""Smoke-test the inference proxy: health, buffered completion, streaming.

    python scripts/smoke_test.py
    python scripts/smoke_test.py --base-url http://127.0.0.1:8000

Exits non-zero if any check fails, so it can gate CI.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx


def check_health(base: str) -> bool:
    print("[1/3] GET /v1/health")
    try:
        r = httpx.get(f"{base}/v1/health", timeout=10)
    except httpx.RequestError as exc:
        print(f"      FAIL  proxy unreachable: {exc}")
        print(f"      Is uvicorn running? `uvicorn api.main:app --port 8000`")
        return False

    body = r.json()
    print(f"      {r.status_code}  {json.dumps(body, indent=6)[6:]}")
    if r.status_code != 200:
        print("      FAIL  upstream not ready -- see docs/INFERENCE.md")
        return False
    print("      OK")
    return True


def check_completion(base: str) -> bool:
    print("\n[2/3] POST /v1/chat/completions  (buffered)")
    payload = {"messages": [{"role": "user", "content": "Reply with exactly: pong"}],
               "max_tokens": 16, "temperature": 0}
    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{base}/v1/chat/completions", json=payload, timeout=300)
    except httpx.RequestError as exc:
        print(f"      FAIL  {exc}")
        return False

    dt = time.perf_counter() - t0
    if r.status_code != 200:
        print(f"      FAIL  {r.status_code}  {r.text[:300]}")
        return False

    body = r.json()
    content = body["choices"][0]["message"]["content"]
    usage = body.get("usage", {})
    print(f"      {r.status_code}  {dt:.2f}s  content={content.strip()!r}")
    print(f"      usage={usage}")
    print("      OK")
    return True


def check_stream(base: str) -> bool:
    print("\n[3/3] POST /v1/chat/completions  (stream=True)")
    payload = {"messages": [{"role": "user", "content": "Count from 1 to 10."}],
               "max_tokens": 64, "temperature": 0, "stream": True}

    chunks = 0
    first_token_at = None
    text_parts: list[str] = []
    t0 = time.perf_counter()

    try:
        with httpx.stream("POST", f"{base}/v1/chat/completions", json=payload, timeout=300) as r:
            if r.status_code != 200:
                print(f"      FAIL  {r.status_code}  {r.read().decode()[:300]}")
                return False

            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data.strip() == "[DONE]":
                    break
                delta = json.loads(data)["choices"][0].get("delta", {})
                piece = delta.get("content")
                if piece:
                    if first_token_at is None:
                        first_token_at = time.perf_counter() - t0
                    chunks += 1
                    text_parts.append(piece)
    except httpx.RequestError as exc:
        print(f"      FAIL  {exc}")
        return False

    total = time.perf_counter() - t0
    if chunks == 0:
        print("      FAIL  no content chunks received")
        return False

    ttft = f"{first_token_at:.2f}s" if first_token_at else "n/a"
    rate = chunks / total if total else 0
    print(f"      {chunks} chunks  time-to-first-token={ttft}  total={total:.2f}s  ~{rate:.1f} chunk/s")
    print(f"      text={''.join(text_parts).strip()[:120]!r}")
    # More than one chunk proves the response really was incremental and not
    # buffered somewhere along the path.
    print("      OK" if chunks > 1 else "      WARN  single chunk -- response may be buffered")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    print(f"Target: {base}\n" + "-" * 60)
    results = [check_health(base)]
    if results[0]:
        results.append(check_completion(base))
        results.append(check_stream(base))

    print("\n" + "-" * 60)
    ok = all(results)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
