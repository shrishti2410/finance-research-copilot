"""Thin abstraction over the vector database.

Intended contents:
- upsert(chunks, vectors, metadata)
- search(query_vector, k, filters)  # filter by company / form / period
- pluggable backend (local for dev, hosted for prod)
"""
