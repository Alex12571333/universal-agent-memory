"""API key registry domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    """Metadata for one configured API key without storing its secret."""

    tenant_id: UUID
    name: str
    secret_fingerprint: str
    scopes: tuple[str, ...]
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str = ""

    def __post_init__(self) -> None:
        """Validate registry rows before they enter storage."""
        if not self.name.strip():
            raise ValueError("api key name must not be empty")
        if not self.secret_fingerprint.strip():
            raise ValueError("api key fingerprint must not be empty")
        if not self.scopes:
            raise ValueError("api key scopes must not be empty")
        normalized = tuple(scope.strip().lower() for scope in self.scopes if scope.strip())
        if not normalized:
            raise ValueError("api key scopes must not be empty")
        object.__setattr__(self, "scopes", normalized)

    @property
    def revoked(self) -> bool:
        """Return whether this key has been explicitly revoked."""
        return self.revoked_at is not None

