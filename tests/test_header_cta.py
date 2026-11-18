"""The header's button never repeats the Research tab (live-site feedback,
2026-11-18): signed in, it reads "New question" on /research and "Dashboard"
everywhere else; signed out, "Try the demo".
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"
DEMO = (SRC / "components" / "DemoButton.tsx").read_text(encoding="utf-8")
RESEARCH = (SRC / "app" / "research" / "page.tsx").read_text(encoding="utf-8")


def _compact_block():
    # The signed-in compact (header) branch.
    i = DEMO.index("if (token)")
    return DEMO[i:DEMO.index("return (", DEMO.index("return (", i) + 1)]


def test_signed_in_header_button_is_new_question_on_research_and_dashboard_elsewhere():
    block = _compact_block()
    assert 'usePathname' in DEMO
    assert '"New question"' in block and '"/research?new=1"' in block
    assert '"Dashboard"' in block and '"/dashboard"' in block
    # The header never offers "Research", which the nav tab already does.
    assert not re.search(r'compact\s*\?\s*"Research"', DEMO)


def test_research_starts_an_empty_conversation_on_new_1():
    assert 'search.get("new")' in RESEARCH
    i = RESEARCH.index('search.get("new")')
    effect = RESEARCH[i:i + 900]
    assert "setActiveId(null)" in effect and "items: []" in effect
    assert 'router.replace("/research")' in effect
