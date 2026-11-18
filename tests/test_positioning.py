"""Research is the main product; signals, backtests and options are the
"Strategy audit" module (decided 2026-11-18). Wording only: no feature is
removed, and every page keeps a link.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"
CHROME = (SRC / "components" / "SiteChrome.tsx").read_text(encoding="utf-8")
LANDING = (SRC / "app" / "page.tsx").read_text(encoding="utf-8")
ABOUT = (SRC / "app" / "about" / "page.tsx").read_text(encoding="utf-8")
SITE = (SRC / "lib" / "site.ts").read_text(encoding="utf-8")


def _nav():
    block = CHROME[CHROME.index("const NAV = ["):]
    block = block[:block.index("];")]
    return re.findall(r'href: "([^"]+)", label: "([^"]+)"', block)


def test_the_nav_puts_research_first_then_the_strategy_audit():
    assert _nav() == [("/research", "Research"), ("/dashboard", "Strategy audit")]


def test_the_hero_leads_with_research():
    h1 = re.search(r"<h1[^>]*>\s*(.*?)\s*</h1>", LANDING, re.S).group(1)
    assert h1.lower().startswith("research")
    assert "trading strateg" not in h1.lower()


def test_strategy_audit_is_named_on_the_landing_page_about_and_in_the_description():
    for name, text in (("landing", LANDING), ("about", ABOUT), ("site", SITE)):
        assert re.search(r"[Ss]trategy audit", text), name


def test_no_feature_loses_its_link():
    for href in ("/research", "/dashboard", "/backtest", "/about"):
        assert f'href: "{href}"' in CHROME, href
    for href in ("/research", "/chart", "/dashboard", "/backtest", "?tab=options"):
        assert href in ABOUT, href


DEMO = (SRC / "components" / "DemoButton.tsx").read_text(encoding="utf-8")
LOGIN = (SRC / "app" / "login" / "page.tsx").read_text(encoding="utf-8")


def test_the_demo_opens_research_where_the_example_questions_are():
    # Decided 2026-11-18. The research page shows its example questions when a
    # conversation is empty (EXAMPLES in app/research/page.tsx).
    assert '"/dashboard"' not in DEMO
    assert DEMO.count('"/research"') == 2              # the signed-in link and the demo sign-in
    demo_block = LOGIN[LOGIN.index("await demo();"):][:120]
    assert 'router.replace("/research")' in demo_block
    assert "EXAMPLES.map" in (SRC / "app" / "research" / "page.tsx").read_text(encoding="utf-8")
    assert "opens Research" in LANDING


def test_the_site_claims_provenance_not_verification():
    # 2026-11-18: the guard enforces that every figure traces to a tool result
    # (provenance); claim-level verification is in progress. The site says so.
    import re as _re
    texts = {p: p.read_text(encoding="utf-8")
             for p in SRC.rglob("*") if p.suffix in (".tsx", ".ts", ".txt") and p.is_file()}
    bad = _re.compile(r"checker|verifies (each|every) figure|every (number|figure)[^.\n]*\bchecked\b"
                      r"|is checked against", _re.I)
    hits = [(str(p.relative_to(SRC)), m.group(0)) for p, t in texts.items() for m in bad.finditer(t)]
    assert hits == []
    assert "traced to its source" in LANDING
