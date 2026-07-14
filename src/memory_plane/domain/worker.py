"""Durable worker liveness records and aggregate readiness state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class WorkerHeartbeat:
    """Latest liveness assertion for one worker process identity."""

    tenant_id: UUID
    worker_kind: str
    worker_id: str
    started_at: datetime
    last_seen_at: datetime
    status: str = "running"

    def __post_init__(self) -> None:
        kind = self.worker_kind.strip().lower()
        worker_id = self.worker_id.strip()
        if not kind or len(kind) > 64:
            raise ValueError("worker_kind must contain between 1 and 64 characters")
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must contain between 1 and 128 characters")
        if self.status not in {"running", "stopping"}:
            raise ValueError("worker heartbeat status must be running or stopping")
        if self.started_at.tzinfo is None or self.last_seen_at.tzinfo is None:
            raise ValueError("worker heartbeat timestamps must be timezone-aware")
        object.__setattr__(self, "worker_kind", kind)
        object.__setattr__(self, "worker_id", worker_id)


@dataclass(frozen=True, slots=True)
class WorkerKindReadiness:
    """Aggregate replica state for one required worker kind."""

    worker_kind: str
    fresh_instances: int = 0
    stale_instances: int = 0

    @property
    def ready(self) -> bool:
        """Return whether at least one running replica has a fresh heartbeat."""
        return self.fresh_instances > 0


@dataclass(frozen=True, slots=True)
class WorkerReadiness:
    """Tenant-scoped aggregate of every required asynchronous worker kind."""

    required: tuple[WorkerKindReadiness, ...] = ()
    observed_at: datetime = field(
        default_factory=lambda: datetime.min.replace(tzinfo=UTC)
    )

    @property
    def ready(self) -> bool:
        """Return whether every configured worker kind has a fresh replica."""
        return all(row.ready for row in self.required)

    @property
    def missing_kinds(self) -> tuple[str, ...]:
        """Return kinds with no recorded process identity at all."""
        return tuple(
            row.worker_kind
            for row in self.required
            if row.fresh_instances == 0 and row.stale_instances == 0
        )

    @property
    def stale_kinds(self) -> tuple[str, ...]:
        """Return kinds observed previously but lacking a fresh running replica."""
        return tuple(
            row.worker_kind
            for row in self.required
            if row.fresh_instances == 0 and row.stale_instances > 0
        )
