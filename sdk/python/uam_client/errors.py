"""Typed HTTP failures exposed by the Python SDK."""

from __future__ import annotations


class MemoryServerError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class InvalidRequestError(MemoryServerError):
    pass


class AuthenticationError(MemoryServerError):
    pass


class PermissionDeniedError(MemoryServerError):
    pass


class NotFoundError(MemoryServerError):
    pass


class ConflictError(MemoryServerError):
    pass


class RateLimitError(MemoryServerError):
    pass


class ServiceUnavailableError(MemoryServerError):
    pass
