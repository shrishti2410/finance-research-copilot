#!/usr/bin/env bash
#
# The concurrency sweep: 5, 10, 20 simultaneous users against the full stack.
#
#     python benchmarks/provision_users.py --users 20    # once, first
#     bash benchmarks/run_sweep.sh
#
# Each level runs for a fixed wall-clock duration rather than a fixed number of
# requests. With one turn taking minutes, a fixed count would make the 20-user
# level take four times as long as the 5-user one and leave the comparison
# confounded by how long the box had been hot.
#
# DURATION is per level, so the whole sweep is 3x that plus ramp.

#
# FRESH=1 provisions brand-new accounts first. Use it for any run that will be
# compared against another, because reusing a pool does not reset the
# conversations, and AGENT_HISTORY_MESSAGES replays a window of prior turns: a
# pool that has been swept before sends a much larger prompt for every request.
#
# Measured, and the reason this flag exists: re-running the sweep on a reused pool
# put 14 of 20 conversations at or past the 10-message window (one had 62
# messages), and single-user median latency read 250s against the 88s the same
# code measured on a fresh pool. Nothing about the system had changed.

set -euo pipefail

HOST="${HOST:-http://127.0.0.1:8000}"
DURATION="${DURATION:-20m}"
LEVELS="${LEVELS:-5 10 20}"
FRESH="${FRESH:-0}"
POOL_SIZE="${POOL_SIZE:-20}"
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/results"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$OUT"

if [ "$FRESH" = "1" ]; then
  echo "provisioning a fresh pool (empty conversations, comparable latency) ..."
  python "$HERE/provision_users.py" --users "$POOL_SIZE" --base-url "$HOST"
  echo
fi

if [ ! -f "$OUT/users.json" ]; then
  echo "No account pool. Run: python benchmarks/provision_users.py --users 20" >&2
  echo "Or re-run with FRESH=1 to provision one now." >&2
  exit 1
fi

echo "host      $HOST"
echo "levels    $LEVELS"
echo "duration  $DURATION per level"
echo

for users in $LEVELS; do
  tag=$(printf "c%02d" "$users")
  echo "=============================================================="
  echo " concurrency $users   ($DURATION)   -> $OUT/$tag"
  echo "=============================================================="

  # Fresh tokens before every level. JWT_EXPIRE_MINUTES is 30 and this sweep
  # runs longer than that, and an expired token does not fail cleanly: the rate
  # limiter can no longer identify a user, falls back to the anonymous per-IP
  # bucket of 20/min, and a harness with no think time turns into a 429
  # generator. The first attempt at this sweep produced ~70,000 of them and a
  # latency table that still looked plausible.
  echo "refreshing tokens ..."
  python "$(dirname "${BASH_SOURCE[0]}")/provision_users.py" --refresh --base-url "$HOST"

  # -r equal to -u: all users arrive at once. This is a saturation test, and a
  # gradual ramp would spend most of the window at a concurrency nobody asked
  # for.
  locust -f "$(dirname "${BASH_SOURCE[0]}")/locustfile.py" \
    --headless -u "$users" -r "$users" -t "$DURATION" \
    --host "$HOST" --csv "$OUT/$tag" --only-summary || true

  # Let the inference queue drain before the next level, so its first requests
  # are not competing with the previous level's leftovers.
  echo "draining for 60s ..."
  sleep 60
done

echo
echo "done. summarize with:  python benchmarks/summarize_sweep.py"
