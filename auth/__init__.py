"""Authentication: password hashing, JWT issuance, and the current-user dependency.

Knows about `db/` (to look a user up) but nothing about conversations or the
agent. `api/` imports from here; nothing here imports from `api/`.
"""
