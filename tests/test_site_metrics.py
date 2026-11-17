"""Every measured number on the site is quoted from a committed benchmark record.

The measured figures are shown on /methodology (the landing page carries none).

frontend/src/content/site-metrics.json lists each figure with its source file and
the exact text it comes from. If a record is rewritten and the quote no longer
appears, this fails, and the landing page has to be updated from the new
measurement rather than keep showing the old one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METRICS = json.loads((ROOT / "frontend/src/content/site-metrics.json").read_text(encoding="utf-8"))


def test_every_quote_is_in_its_source_record():
    assert METRICS["metrics"]
    for m in METRICS["metrics"]:
        source = ROOT / m["source"]
        assert source.is_file(), m["source"]
        assert m["quote"] in source.read_text(encoding="utf-8"), (m["value"], m["source"])


def test_every_shown_value_appears_in_its_quote():
    for m in METRICS["metrics"]:
        digits = re.findall(r"\d[\d,.]*\d|\d", m["value"])
        assert digits and all(d in m["quote"] for d in digits), (m["value"], m["quote"])


def test_every_other_figure_on_a_card_is_quoted_too():
    """A card's detail and secondary line may carry more figures; each needs
    its own quote from the same record, listed under "also"."""
    for m in METRICS["metrics"]:
        text = (ROOT / m["source"]).read_text(encoding="utf-8")
        also = {a["text"]: a["quote"] for a in m.get("also", [])}
        for a in m.get("also", []):
            assert a["quote"] in text, (m["value"], a["quote"])
            for d in re.findall(r"\d[\d,.]*\d|\d", a["text"]):
                assert d in a["quote"], (a["text"], a["quote"])
        shown = m["detail"] + " " + m.get("secondary", {}).get("value", "")
        # Stand-alone figures only: not the 120 in "gpt-oss-120b" or the 40 in
        # "40-question".
        for figure in re.findall(r"(?<![\w-])\d[\d,.]*\d(?: s|%)?(?![\w-])", shown):
            if figure in (m["value"], "10-K") or figure.isdigit() and len(figure) <= 2:
                continue                 # counts in prose: "40-question", "15 US companies"
            assert any(figure in t for t in also), (m["value"], figure)


PAGES = ROOT / "frontend/src/app"


def test_the_methodology_page_renders_every_metric_from_the_quoted_file():
    page = (PAGES / "methodology/page.tsx").read_text(encoding="utf-8")
    assert 'from "@/content/site-metrics.json"' in page
    assert "metrics.metrics.map(" in page


def test_the_landing_page_states_no_measurement_of_its_own():
    """Accuracy, latency and backtest figures belong on /methodology, where the
    tests above tie each to its record. The landing page may name counts of
    what is covered ("15 US companies") but no score, rate, duration or ratio."""
    page = (PAGES / "page.tsx").read_text(encoding="utf-8")
    assert "site-metrics.json" not in page
    measured = re.findall(r"\d+(?:\.\d+)?\s?(?:%|ms|s|seconds?|minutes?|x)|\d+\s?/\s?\d+|\d+ of \d+",
                          page)
    assert measured == [], measured
