"""Turn raw filing documents into clean, structured text.

Intended contents:
- strip filing HTML boilerplate; keep section structure (Item 1A, MD&A, ...)
- extract tables and XBRL financial facts
- normalize whitespace, footnote markers, page artifacts
"""
