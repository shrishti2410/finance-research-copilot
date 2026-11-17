"""The keep-warm workflow: manual only until the user enables its schedule.

Enabling it is a decision about Render's free instance hours (750 per workspace
a month) and terms; see the comment at the top of the workflow. These tests
keep it from starting to ping by accident, and pin what it may touch. Read as
text, so the suite needs no YAML parser.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "keep-warm.yml"
LINES = WORKFLOW.read_text(encoding="utf-8").splitlines()
LIVE = [line for line in LINES if line.strip() and not line.lstrip().startswith("#")]


def test_it_runs_only_by_hand_until_the_schedule_is_enabled():
    assert any(re.match(r"^\s+workflow_dispatch:\s*$", line) for line in LIVE)
    assert not any(re.match(r"^\s*(schedule|- cron):", line.strip() + ":") or "cron:" in line
                   for line in LIVE), (
        "the keep-warm schedule is a decision for the user (Render's free hours and terms); "
        "update this test in the same commit that enables it")


def test_it_pings_only_health_and_holds_no_permissions():
    assert "permissions: {}" in LIVE
    text = "\n".join(LIVE)
    assert '"$API/health"' in text and "/ask" not in text and "/market" not in text
