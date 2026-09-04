"""Wire the ingestion stages together and write to the vector store.

Intended contents:
- orchestrate edgar_client -> parser -> chunker -> embeddings -> vector store
- incremental refresh (skip already-indexed accession numbers)
- CLI entrypoint for scheduled runs
"""
