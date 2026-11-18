"""The keep-warm workflow: 02:30 to 18:30 UTC, /health only.

Enabled 2026-11-18 by the user (the API is the workspace's only Render service;
the hours arithmetic is in the workflow's header and CLAUDE.md). These tests
pin the window, the gap between pings and what the workflow may touch. Read as
text, so the suite needs no YAML parser.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "keep-warm.yml"
LIVE = [line for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")]


def _expand(field: str, lo: int, hi: int) -> list[int]:
    out = []
    for part in field.split(","):
        if part.startswith("*/"):
            out += list(range(lo, hi + 1, int(part[2:])))
        elif "-" in part:
            a, b = map(int, part.split("-"))
            out += list(range(a, b + 1))
        else:
            out.append(int(part))
    return out


def _pings() -> list[int]:
    """Minutes after midnight UTC of every scheduled run."""
    times = []
    for line in LIVE:
        m = re.search(r'cron:\s*"([^"]+)"', line)
        if m:
            minute, hour, *rest = m.group(1).split()
            assert rest == ["*", "*", "*"], "every day of the month, month and week"
            times += [h * 60 + mi for h in _expand(hour, 0, 23) for mi in _expand(minute, 0, 59)]
    return sorted(times)


def test_it_pings_from_0230_to_1830_utc_never_more_than_14_minutes_apart():
    t = _pings()
    assert t[0] == 2 * 60 + 30 and 18 * 60 + 15 <= t[-1] <= 18 * 60 + 30
    assert max(b - a for a, b in zip(t, t[1:])) <= 14
    awake_hours = (t[-1] + 15 - t[0]) / 60
    assert awake_hours * 31 <= 750 - 200, "leave room in Render's 750 free hours a month"


def test_it_pings_only_health_never_readiness_and_holds_no_permissions():
    assert "permissions: {}" in LIVE
    text = "\n".join(LIVE)
    assert '"$API/health"' in text
    for path in ("/health/ready", "/health/db", "/ask", "/market"):
        assert path not in text
