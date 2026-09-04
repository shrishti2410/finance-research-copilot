"""Split parsed filings into retrieval units.

Intended contents:
- section-aware chunking with overlap
- attach metadata to each chunk: company, CIK, form type, fiscal period,
  filing date, section, source URL
"""
