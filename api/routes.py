"""HTTP routes for the copilot.

Intended contents:
- POST /ask       -> single-shot question, returns answer + citations
- POST /ask/stream -> streamed tokens/events
- session endpoints as needed
Each handler validates input, calls `agent/`, and shapes the response.
"""
