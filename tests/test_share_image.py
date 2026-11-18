"""The link-preview card is drawn in code (app/opengraph-image.tsx, next/og),
not a screenshot, and says what the landing page says (2026-11-18).

This file checks the source. That the route builds and serves a 1200x630 PNG
is checked against a running build by frontend/scripts/check-share-image.mjs
(CI does not build the frontend, and a skip here would fail CI).
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "frontend" / "src" / "app"
SITE = (APP.parent / "lib" / "site.ts").read_text(encoding="utf-8")
OG = (APP / "opengraph-image.tsx").read_text(encoding="utf-8")
TW = (APP / "twitter-image.tsx").read_text(encoding="utf-8")
LANDING = (APP / "page.tsx").read_text(encoding="utf-8")


def _const(name):
    return re.search(rf'export const {name} = "([^"]+)"', SITE).group(1)


def test_the_card_text_is_the_landing_headline():
    headline = _const("HEADLINE")
    assert headline == ("Research public companies from their filings, "
                        "with every figure traced to its source")
    assert re.search(r"<h1[^>]*>\s*\{HEADLINE\}\s*</h1>", LANDING)      # the page shows it
    assert "{HEADLINE}" in OG and "HEADLINE" in TW                       # the card shows it


def test_the_card_has_the_three_entry_points_and_no_screenshot_or_nav():
    entries = re.search(r"export const ENTRY_POINTS = \[([^\]]+)\]", SITE).group(1)
    assert re.findall(r'"([^"]+)"', entries) == ["Research a company", "Compare companies", "Test a strategy"]
    assert "ENTRY_POINTS.map" in OG
    for gone in ("<img", "shots/", "webp", "Methodology", "Signals", "Sign in"):
        assert gone not in OG, gone


def test_the_card_is_1200_by_630_png_drawn_with_next_og():
    for src in (OG, TW):
        assert re.search(r"export const size = \{ width: 1200, height: 630 \}", src)
        assert 'export const contentType = "image/png"' in src
    assert 'import { ImageResponse } from "next/og"' in OG
    assert 'from "./opengraph-image"' in TW


def test_the_static_images_are_gone():
    for name in ("opengraph-image.png", "twitter-image.png", "opengraph-image.alt.txt", "twitter-image.alt.txt"):
        assert not (APP / name).exists(), name
