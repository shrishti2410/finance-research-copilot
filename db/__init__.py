"""Persistence layer: engine, session lifecycle, and the ORM models.

Owns no HTTP or business logic. `api/` and `auth/` acquire an `AsyncSession`
through `get_session`; everything else talks to models, never to the engine.
"""
