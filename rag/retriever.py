"""Query-to-context pipeline used by the agent.

Intended contents:
- expand/rewrite the query, resolve the target company
- vector search with metadata filters
- optional re-ranking
- assemble a context block with per-passage citations (filing, section, URL)
"""
