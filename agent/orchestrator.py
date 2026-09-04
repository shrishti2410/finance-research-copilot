"""The agent loop.

Intended contents:
- accept a normalized request (question, session, history)
- plan required sources; call retriever and tools; observe results
- iterate until it can answer; produce answer + structured citations
- enforce step/token budgets
"""
