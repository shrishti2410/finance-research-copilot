"""Annual facts out of SEC's XBRL company-facts JSON.

    payload = EdgarClient().company_facts("NVDA")
    rows = annual_facts(payload, "NVDA")

`data.sec.gov/api/xbrl/companyfacts/CIK##########.json` is every tagged number
a company has filed, in one document:

    facts -> taxonomy ("us-gaap", "dei") -> concept ("Revenues")
          -> units -> unit ("USD", "shares", "USD/shares") -> [entries]

and an entry is one number as one filing reported it:

    {"start": "2025-01-27", "end": "2026-01-25", "val": 215938000000,
     "accn": "0001045810-26-000021", "fy": 2026, "fp": "FY",
     "form": "10-K", "filed": "2026-02-25"}

This module turns that into one row per company, concept, unit and fiscal year.
Nothing here touches the network or the database, so it is tested on plain
dictionaries.

Three things in the data are not what they look like
----------------------------------------------------
**`fy` is the filing's fiscal year, not the number's.** A 10-K prints three
years of income statement, and all three columns arrive with the `fy` of the
filing that printed them. NVIDIA's fiscal 2024 revenue appears with fy=2024,
fy=2025 and fy=2026. Reading `fy` as the period's year would file three
different revenues under 2026. The fiscal year of a number is decided by its
period end instead: the year label of a period ending on date E is the smallest
`fy` any annual report gave a full-year figure ending on E, because the report
*for* that year is the first to print it and later reports only repeat it.

**The same number is reported more than once.** Each later 10-K repeats the
prior years, sometimes restated: NVIDIA's fiscal 2024 EPS was $11.93 when filed
and is $1.19 in every report since its ten-for-one split. One row is kept per
fiscal year, and it is the most recently filed one, because that is the figure
comparable with everything else the company now reports. The accession number
of the filing it came from is kept with it, so the choice can be checked.

**A duration and an instant share a shape.** Revenue covers a year (`start` and
`end`); total assets is a balance on a day (`end` only). Durations are kept
when they span a fiscal year, 350 to 380 days, which also drops the quarterly
and year-to-date figures a 10-K carries. Instants are kept when their date is
one of the company's fiscal year ends.

What is left out
----------------
Quarterly figures, anything not from an annual report, and anything with a
dimension: company-facts holds only facts reported for the whole entity, so
segment figures such as Apple's Services net sales are not in it at all. Those
stay with the filing text; see `ingestion/chunker.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = ["FactRow", "annual_facts", "ANNUAL_FORMS"]

# Annual reports. The amendments are included because an amended figure is the
# company's own correction of the original.
ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"})

# A fiscal year is 52 or 53 weeks, or a calendar year: 364, 371, 365 or 366
# days. The band is wider than that only by what a short stub would need to be
# mistaken for one, which it is not.
_YEAR_DAYS = range(350, 381)


@dataclass(frozen=True)
class FactRow:
    """One number, for one company, concept, unit and fiscal year."""

    cik: str
    ticker: str
    taxonomy: str
    concept: str
    unit: str
    fiscal_year: int
    fiscal_period: str          # "FY"; the column exists so quarters can follow
    period_start: date | None   # None for a balance on a day
    period_end: date
    value: float
    accession: str
    form: str
    filed: date


def _day(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _is_full_year(entry: dict) -> bool:
    start, end = _day(entry.get("start")), _day(entry.get("end"))
    return bool(start and end and (end - start).days in _YEAR_DAYS)


def _annual_entries(payload: dict, filed_through: date | None = None):
    """Every entry from an annual report, with where it sits in the document."""
    for taxonomy, concepts in (payload.get("facts") or {}).items():
        for concept, body in (concepts or {}).items():
            for unit, entries in ((body or {}).get("units") or {}).items():
                for entry in entries or []:
                    if entry.get("form") not in ANNUAL_FORMS or entry.get("val") is None:
                        continue
                    if filed_through is not None:
                        filed = _day(entry.get("filed"))
                        if filed is None or filed > filed_through:
                            continue
                    yield taxonomy, concept, unit, entry


def fiscal_year_ends(payload: dict, filed_through: date | None = None) -> dict[date, int]:
    """Fiscal year end date -> the year label the company gives that year.

    See the module docstring: the smallest `fy` among the annual reports that
    printed a full-year figure ending on that date.
    """
    labels: dict[date, int] = {}
    for _, _, _, entry in _annual_entries(payload, filed_through):
        if not _is_full_year(entry) or not isinstance(entry.get("fy"), int):
            continue
        end = _day(entry["end"])
        if end not in labels or entry["fy"] < labels[end]:
            labels[end] = entry["fy"]
    return labels


def annual_facts(payload: dict, ticker: str,
                 filed_through: date | None = None) -> list[FactRow]:
    """One row per concept, unit and fiscal year: the most recently filed figure.

    `filed_through` ignores everything filed after that date. The document
    grows with each new report, and a later report can restate an earlier
    year, so "the facts as of a date" is what stays the same from one run to
    the next; see `PINNED_FACTS_THROUGH` in `ingestion/pipeline.py`.
    """
    cik = str(payload.get("cik", "")).zfill(10)
    year_of = fiscal_year_ends(payload, filed_through)
    chosen: dict[tuple, tuple] = {}

    for taxonomy, concept, unit, entry in _annual_entries(payload, filed_through):
        end = _day(entry.get("end"))
        filed = _day(entry.get("filed"))
        if end is None or filed is None or end not in year_of:
            continue
        start = _day(entry.get("start"))
        if start is not None and not _is_full_year(entry):
            continue  # a quarter or a year-to-date figure inside the annual report
        key = (taxonomy, concept, unit, year_of[end])
        # Most recently filed wins; the accession breaks a same-day tie the same
        # way every time.
        rank = (filed, entry.get("accn") or "")
        if key not in chosen or rank > chosen[key][0]:
            chosen[key] = (rank, start, end, entry)

    rows = []
    for (taxonomy, concept, unit, fiscal_year), (_, start, end, entry) in chosen.items():
        try:
            value = float(entry["val"])
        except (TypeError, ValueError):
            continue
        rows.append(FactRow(
            cik=cik, ticker=ticker.upper(), taxonomy=taxonomy, concept=concept, unit=unit,
            fiscal_year=fiscal_year, fiscal_period="FY",
            period_start=start, period_end=end, value=value,
            accession=entry.get("accn") or "", form=entry.get("form") or "",
            filed=_day(entry["filed"]),
        ))
    rows.sort(key=lambda r: (r.taxonomy, r.concept, r.unit, r.fiscal_year))
    return rows
