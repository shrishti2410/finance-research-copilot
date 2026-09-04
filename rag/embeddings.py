"""Embedding client shared by ingestion and query time.

Intended contents:
- provider-agnostic wrapper (model + key from env)
- batch embedding for ingestion, single-query embedding for retrieval
"""
