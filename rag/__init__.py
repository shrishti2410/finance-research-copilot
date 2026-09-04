"""Retrieval over the indexed SEC filings.

Turns a natural-language query into a ranked set of filing passages plus
assembled context for the agent. Consumes what `ingestion/` wrote to the vector
store; does not build the index itself.
"""
