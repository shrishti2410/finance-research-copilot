"""python -m inference_router --upstream URL [--upstream URL ...] [--port 11400]"""

from __future__ import annotations

import argparse

import uvicorn

from inference_router.app import create_app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Round-robin router for inference replicas.")
    ap.add_argument("--upstream", action="append", required=True,
                    help="server root, e.g. http://127.0.0.1:11434 (repeat per replica)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=11400)
    ap.add_argument("--read-timeout", type=float, default=900.0)
    args = ap.parse_args(argv)

    # Access logging off: one line per request is noise at load-test volume, and
    # /router/stats is the record that matters.
    uvicorn.run(create_app(args.upstream, read_timeout=args.read_timeout),
                host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
