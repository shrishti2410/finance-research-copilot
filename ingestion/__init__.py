"""Offline pipeline that builds and refreshes the SEC-filing index.

Flow: fetch from EDGAR -> parse (HTML/XBRL) -> clean -> chunk -> embed ->
persist to the vector store. Run on a schedule or on new-filing events. Nothing
here is called at request time.
"""
