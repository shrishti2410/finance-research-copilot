"""The decision-making core.

Runs the plan -> act -> observe -> answer loop: builds prompts, routes to
`rag/` and `tools/`, keeps conversation memory, and synthesizes the final cited
answer. `api/` calls into here; this package owns no transport concerns.
"""
