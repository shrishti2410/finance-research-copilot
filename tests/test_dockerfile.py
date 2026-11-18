"""The API image copies every Python package the app is made of.

The Dockerfile copies packages one by one. When version 2 added market/,
nothing failed until the container started: "No module named 'market'"
(found 2026-11-17 by running the image at the free tier's limits).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Packages deliberately left out of the API image, with the reason.
NOT_IN_THE_API_IMAGE = {
    "inference_router": "a separate service (python -m inference_router); the API never imports it",
}


def test_every_package_is_copied_into_the_api_image():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copied = set(re.findall(r"^COPY\s+(\w+)/\s", dockerfile, re.M))
    packages = {p.parent.name for p in ROOT.glob("*/__init__.py")}
    missing = packages - copied - set(NOT_IN_THE_API_IMAGE)
    assert not missing, f"Dockerfile does not COPY: {sorted(missing)}"


def test_the_strategy_cards_are_in_the_image_for_the_indexer():
    assert "COPY docs/strategies/" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_the_server_keeps_idle_connections_longer_than_a_visitor_pauses():
    """uvicorn's default closes a keep-alive connection after 5 idle seconds. In
    the 2026-11-18 load test (benchmarks/LOAD_TEST_MARKET.md) users paused 1-5 s
    between requests, and 5 of 2,242 requests met a connection the server was
    just closing. 15 s outlasts a reader's pause between clicks."""
    from pathlib import Path

    start = (Path(__file__).resolve().parents[1] / "scripts" / "start.sh").read_text(encoding="utf-8")
    assert "--timeout-keep-alive 15" in start
    assert "exec uvicorn api.main:app" in start
