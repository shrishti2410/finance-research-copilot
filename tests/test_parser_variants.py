"""10-K markup the NVDA and AAPL filings do not use, found indexing 15 companies
on 2026-11-16. Synthetic documents, one per variation; nothing is fetched.

Each one stopped the parser finding any section: in 18 of 45 filings it found
no Risk Factors and no MD&A at all.
"""

from __future__ import annotations

from ingestion.parser import find_item_markers, parse_10k

PARAGRAPH = ("Our business depends on many factors that are described at length in "
             "this section, and each of them could harm our results. ") * 3


def _doc(body: str) -> str:
    return f"<html><body>{body}</body></html>"


def _toc() -> str:
    """A table of contents as filings have one: every item, so it runs well past
    the 200 characters a heading may have."""
    rows = "".join(f"<tr><td><a>Item {item}.</a></td><td>{title}</td><td>{page}</td></tr>"
                   for page, (item, title) in enumerate([
                       ("1", "Business"), ("1A", "Risk Factors"),
                       ("1B", "Unresolved Staff Comments"), ("2", "Properties"),
                       ("3", "Legal Proceedings"), ("5", "Market for Common Equity"),
                       ("7", "Management's Discussion and Analysis"),
                       ("7A", "Quantitative and Qualitative Disclosures"),
                       ("8", "Financial Statements and Supplementary Data")], start=3))
    return f"<div><table>{rows}</table></div>"


def test_a_heading_split_across_spans_with_no_space_is_found():
    """Alphabet, Amazon, Meta, Netflix, Walmart: "ITEM 1A." and "RISK FACTORS"
    in separate spans read as "ITEM 1A.RISK FACTORS"."""
    html = _doc(_toc()
                + "<div><span>ITEM 1A.</span><span>RISK FACTORS</span></div>"
                + f"<div>{PARAGRAPH}</div>"
                + "<div><span>ITEM 7.</span><span>MANAGEMENT'S DISCUSSION</span></div>"
                + f"<div>{PARAGRAPH}</div>"
                + "<div><span>ITEM 8.</span><span>FINANCIAL STATEMENTS</span></div>")
    parsed = parse_10k(html)
    assert parsed.risk_factors and "Our business depends" in parsed.risk_factors.text
    assert parsed.mdna and "Our business depends" in parsed.mdna.text


def test_item_1_still_does_not_match_item_1a():
    from lxml.html import fragment_fromstring
    blocks = [fragment_fromstring("<div>Item 1A.Risk Factors</div>"),
              fragment_fromstring("<div>Item 1. Business</div>"),
              fragment_fromstring("<div>Item 10.Directors</div>")]
    markers = find_item_markers(blocks)
    assert markers == {"1A": 0, "1": 1, "10": 2}


def test_headings_below_a_wrapper_div_are_found():
    """Microsoft and JPMorgan: the whole document sits in container divs, so no
    body-level block is a heading."""
    inner = ("<p>ITEM 1A. RISK FACTORS</p>" + f"<p>{PARAGRAPH}</p>"
             + "<p>ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS</p>" + f"<p>{PARAGRAPH}</p>"
             + "<p>ITEM 8. FINANCIAL STATEMENTS</p>")
    for wrapped in (f"<div>{inner}</div>", f"<div><div>{inner}</div><div>x</div></div>"):
        parsed = parse_10k(_doc(_toc() + wrapped))
        assert parsed.risk_factors and parsed.mdna, wrapped[:30]
        assert "Our business depends" in parsed.risk_factors.text


def test_a_bare_running_header_does_not_beat_the_titled_heading():
    """Microsoft prints "Item 1A" alone at the top of every page of the section.
    With "last wins" the marker landed on the last page."""
    pages = "".join(f"<p>Item 1A</p><p>Risk number {i}. {PARAGRAPH}</p>" for i in range(4))
    html = _doc(_toc() + "<div>" + "<p>ITEM 1A. RISK FACTORS</p>" + pages
                + "<p>ITEM 7. MANAGEMENT'S DISCUSSION</p>" + f"<p>{PARAGRAPH}</p>"
                + "<p>ITEM 8. FINANCIAL STATEMENTS</p></div>")
    parsed = parse_10k(html)
    text = parsed.risk_factors.text
    assert all(f"Risk number {i}." in text for i in range(4))


def test_a_bare_label_is_still_used_when_there_is_no_titled_heading():
    html = _doc("<div>Item 1A</div>" + f"<div>{PARAGRAPH}</div>"
                + "<div>Item 7</div>" + f"<div>{PARAGRAPH}</div><div>Item 8</div>")
    parsed = parse_10k(html)
    assert parsed.risk_factors and parsed.mdna
