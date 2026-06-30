"""Infrastructure adapters; each may be replaced without changing services."""

from memory_plane.adapters.in_memory import InMemoryMemoryStore

__all__ = ["InMemoryMemoryStore"]
