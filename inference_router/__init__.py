"""Round-robin router in front of identical inference replicas. See app.py."""

from inference_router.app import RoundRobin, create_app

__all__ = ["RoundRobin", "create_app"]
