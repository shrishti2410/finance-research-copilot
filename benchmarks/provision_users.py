"""Create the accounts the load test will use, before it starts.

    python benchmarks/provision_users.py --users 20

Written as a separate step for a reason that is itself a finding: `/auth/*` is
rate limited to 10 requests per minute **per IP, regardless of token**, because
login and signup are the brute-force surface. Every simulated user comes from
127.0.0.1, so twenty of them signing up at the moment load starts means the
eleventh gets a 429 and the test measures the rate limiter instead of the agent.

Pacing the signups here keeps the limiter out of the measurement, and is also the
more realistic shape: real users do not all register at the instant traffic
arrives.

Writes benchmarks/results/users.json, which the locustfile reads.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import uuid

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASSWORD = "load-test-correct-horse-battery"
OUT = pathlib.Path(__file__).parent / "results" / "users.json"

# The limit is 10/min on /auth/*, and it is a **sliding window log**
# (`api/rate_limit.py`), not a fixed window. That distinction cost two failed
# refresh attempts: bursting 8 requests and then sleeping 61s does not free the
# budget, because the sliding window still counts those 8 until each one
# individually ages out. Only 10 and 12 of 20 logins succeeded.
#
# A steady trickle below the rate never trips a sliding window, so pace one
# request every SPACING seconds instead of bursting.
PER_MINUTE = 8
SPACING = 60.0 / PER_MINUTE      # 7.5s between /auth calls


def provision(base: str, count: int, timeout: float) -> list[dict]:
    client = httpx.Client(base_url=base, timeout=timeout)
    accounts: list[dict] = []
    last_auth = 0.0

    for i in range(count):
        gap = SPACING - (time.perf_counter() - last_auth)
        if gap > 0:
            time.sleep(gap)
        last_auth = time.perf_counter()

        email = f"load-{uuid.uuid4().hex[:12]}@example.com"
        signup = client.post("/auth/signup", json={"email": email, "password": PASSWORD})
        if signup.status_code != 201:
            print(f"  {i + 1:>3}  signup FAILED {signup.status_code}: {signup.text[:120]}")
            if signup.status_code == 429:
                print("       rate limited despite pacing -- lower PER_MINUTE")
            continue

        token = signup.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Each user gets one conversation and keeps it, so history accumulates
        # the way it does for a real user -- which matters, because the agent
        # replays a window of prior messages and that grows the prompt.
        conversation = client.post("/conversations", json={}, headers=headers)
        if conversation.status_code != 201:
            print(f"  {i + 1:>3}  conversation FAILED {conversation.status_code}")
            continue

        accounts.append({
            "email": email,
            "token": token,
            "conversation_id": conversation.json()["id"],
        })
        print(f"  {i + 1:>3}  {email}  ok")

    client.close()
    return accounts


def refresh(base: str, timeout: float) -> list[dict]:
    """Log the existing accounts in again, replacing expired tokens.

    Needed because JWT_EXPIRE_MINUTES is 30 and a three-level sweep runs longer
    than that. The first attempt at this sweep was invalidated by exactly that:
    tokens expired mid-run, requests became unauthenticated, the rate limiter
    could no longer identify a user and fell back to the anonymous per-IP bucket
    (20/min), and the harness -- which has no think time -- turned into a 429
    generator. Roughly 70,000 of them in five minutes, and the latency table it
    produced looked perfectly plausible.

    So the sweep refreshes before every level rather than trusting a token to
    outlive the run.
    """
    if not OUT.exists():
        print(f"No pool at {OUT}; nothing to refresh. Run without --refresh first.")
        return []

    accounts = json.loads(OUT.read_text(encoding="utf-8"))
    client = httpx.Client(base_url=base, timeout=timeout)
    refreshed = 0
    last_auth = 0.0

    print(f"  pacing one login every {SPACING:.1f}s "
          f"({len(accounts)} accounts, about {len(accounts) * SPACING / 60:.1f} min)")

    for account in accounts:
        gap = SPACING - (time.perf_counter() - last_auth)
        if gap > 0:
            time.sleep(gap)
        last_auth = time.perf_counter()

        login = client.post("/auth/login",
                            json={"email": account["email"], "password": PASSWORD})
        if login.status_code == 200:
            account["token"] = login.json()["access_token"]
            refreshed += 1
        else:
            print(f"  login FAILED for {account['email']}: {login.status_code}")

    client.close()
    print(f"  refreshed {refreshed} of {len(accounts)} token(s)")
    return accounts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--users", type=int, default=20,
                    help="size of the pool; make it the largest concurrency you will run")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--refresh", action="store_true",
                    help="re-log-in the existing pool instead of creating one")
    args = ap.parse_args()

    if args.refresh:
        print(f"refreshing tokens against {args.base_url}")
        accounts = refresh(args.base_url.rstrip("/"), args.timeout)
        if not accounts:
            return 1
        OUT.write_text(json.dumps(accounts, indent=2), encoding="utf-8")
        print(f"wrote {len(accounts)} account(s) to {OUT}")
        return 0

    print(f"provisioning {args.users} account(s) against {args.base_url}")
    accounts = provision(args.base_url.rstrip("/"), args.users, args.timeout)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(accounts, indent=2), encoding="utf-8")
    print(f"\nwrote {len(accounts)} account(s) to {OUT}")

    if len(accounts) < args.users:
        print(f"WARNING: asked for {args.users}, got {len(accounts)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
