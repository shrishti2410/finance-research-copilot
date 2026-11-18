"""Every measured figure the project publishes is in the README, quoted from the
benchmark record its table row cites; the site states none.

Until 2026-11-18 the site had a /methodology page with its own copy of the
figures (frontend/src/content/site-metrics.json). It was removed: results now
live in the README and benchmarks/ only, and this test checks the README the
way the old one checked that file.

The rule: in a README table, a row whose last cell links a record under
benchmarks/ or docs/ (or says "same", meaning the row above's record) cites
that record, and every figure in the row's other cells appears in it, written
the same way. Figures inside `code` (commits, settings) are not checked. The
first run of this test found the "Noise" row citing the wrong record (a "same"
left pointing at a row inserted above it); see the commit that added it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
PAGES = ROOT / "frontend" / "src" / "app"

# A figure: a number with what it is wearing -- a sign, grouping, decimals, a
# percent sign, seconds, a size unit, "of N" or "/N".
FIGURE = re.compile(r"(?<![\w.`/-])[−-]?\d[\d,]*(?:\.\d+)?(?:\s?%|\s?s\b|\s?MiB|\s?MB| of \d+|/\d+)?(?![\w`])")


def _cited_rows():
    """(line number, record path, row text) for every README table row that
    cites a record."""
    rows, source = [], None
    for n, line in enumerate(README.splitlines(), 1):
        if not line.startswith("|"):
            source = None
            continue
        if set(line.strip()) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        link = re.search(r"\]\(((?:benchmarks|docs)/[^)#]+\.md)", cells[-1])
        if link:
            source = link.group(1)
        elif cells[-1].lower() != "same":
            continue
        if source:
            rows.append((n, source, " ".join(cells[:-1])))
    return rows


ROWS = _cited_rows()


def _norm(text: str) -> str:
    return text.replace("−", "-")


def test_the_readme_has_cited_results():
    assert len(ROWS) >= 10
    assert {"benchmarks/QUANT_HONESTY.md", "benchmarks/HOSTED_RUN_2026-11-16.md",
            "benchmarks/STEP4_CORPUS.md"} <= {s for _, s, _ in ROWS}


@pytest.mark.parametrize("line, source, row", ROWS, ids=[f"line{n}" for n, _, _ in ROWS])
def test_every_figure_in_a_row_is_in_the_record_it_cites(line, source, row):
    record = ROOT / source
    assert record.is_file(), f"README line {line} cites {source}, which does not exist"
    text = _norm(record.read_text(encoding="utf-8"))
    body = re.sub(r"`[^`]*`", "", _norm(row))
    missing = [f.strip() for f in FIGURE.findall(body) if f.strip() not in text]
    assert not missing, f"README line {line}: {missing} not found in {source}"


# Figures on the site that are not this project's measurements, each with why.
NOT_MEASUREMENTS = {
    "15 minutes": "Render's documented rule: a free service sleeps after 15 minutes without traffic",
}


def _visible(text: str) -> str:
    """The page source without code comments and class names, which hold
    numbers that are not claims (a 120ms animation delay)."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", " ", text)
    return re.sub(r'className=(?:"[^"]*"|\{`[^`]*`\})', " ", text)


@pytest.mark.parametrize("page", ["page.tsx", "about/page.tsx"])
def test_the_site_states_no_measurement(page):
    """Counts of what is covered ("15 US companies") are fine; a score, rate,
    duration or ratio is a result, and results live in the README. (The check
    this replaces had a doubled backslash in its pattern and matched nothing.)"""
    text = _visible((PAGES / page).read_text(encoding="utf-8"))
    measured = re.findall(r"\d+(?:\.\d+)?\s?(?:%|ms\b|s\b|seconds?\b|minutes?\b|x\b)|\b\d+\s?/\s?\d+\b|\b\d+ of \d+\b",
                          text)
    assert [m for m in measured if m not in NOT_MEASUREMENTS] == [], (page, measured)


def test_the_measurement_check_catches_a_measurement():
    assert re.findall(r"\d+(?:\.\d+)?\s?(?:%|ms\b|s\b)", _visible("<p>median 2.9 s, 36% better</p>"))


def test_the_methodology_page_is_gone_and_nothing_links_to_it():
    assert not (PAGES / "methodology").exists()
    assert not (ROOT / "frontend" / "src" / "content" / "site-metrics.json").exists()
    for path in (ROOT / "frontend" / "src").rglob("*.ts*"):
        assert "/methodology" not in path.read_text(encoding="utf-8"), path
