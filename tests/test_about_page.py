"""/about describes the site for a visitor (rewritten 2026-11-18): what it is,
what you can do, what is covered, the data, the principles, and who built it.
How it is built stays in the README.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "frontend" / "src" / "app"
ABOUT = (APP / "about" / "page.tsx").read_text(encoding="utf-8")
SITE = (ROOT / "frontend" / "src" / "lib" / "site.ts").read_text(encoding="utf-8")


def test_the_six_sections_are_there_in_order():
    titles = ["What this is", "What you can do", "What's covered", "Data and freshness",
              "Principles", "Built by {AUTHOR.name}"]
    found = [ABOUT.find(t) for t in titles]
    assert all(i >= 0 for i in found), dict(zip(titles, found))
    assert found == sorted(found)


def test_every_feature_links_to_a_page_that_exists():
    hrefs = re.findall(r'href: "(/[^"?]*)', ABOUT)
    assert len(hrefs) == 5
    for href in hrefs:
        route = APP / href.strip("/").split("/")[0]
        assert route.is_dir(), href


def test_how_it_is_built_is_not_on_the_page_but_is_in_the_readme():
    for gone in ("How it&apos;s built", "GUARDS", "pgvector", "FastAPI", "Ollama"):
        assert gone not in ABOUT, gone
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Architecture" in readme and "Grounding guard" in readme


def test_profile_links_the_author_has_not_sent_stay_empty():
    # Shown as "coming soon"; never an invented address.
    assert re.search(r'linkedin: ""', SITE) and re.search(r'resume: ""', SITE)
    assert "coming soon" in ABOUT


def test_it_says_it_is_not_investment_advice():
    assert "not investment advice" in ABOUT
