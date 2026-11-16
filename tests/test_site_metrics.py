"""Every number on the landing page is quoted from a committed benchmark record.

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
        digits = re.findall(r"\d[\d,]*", m["value"])
        assert digits and all(d in m["quote"] for d in digits), (m["value"], m["quote"])
