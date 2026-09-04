"""Fetch filings and metadata from SEC EDGAR.

Intended contents:
- resolve ticker/name -> CIK
- list filings for a company (form type, period, accession number)
- download filing documents, respecting EDGAR's User-Agent and rate limits
"""
