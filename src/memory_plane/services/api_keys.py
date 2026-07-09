"""Service for API key metadata, rotation state and last-used tracking."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from memory_plane.domain.api_key import ApiKeyRecord
from memory_plane.ports.repositories import ApiKeyRegistryRepository


class ApiKeyRegistryService:
    """Manage key metadata without storing bearer secrets."""

    def __init__(self, repository: ApiKeyRegistryRepository) -> None:
        """Retain repository; no external I/O at construction time."""
        self._repository = repository

    def ensure_configured_key(
        self,
        tenant_id: UUID,
        *,
        name: str,
        secret_fingerprint: str,
        scopes: tuple[str, ...],
    ) -> ApiKeyRecord:
        """Create/update registry metadata for one env-configured key."""
        existing = self._repository.get_api_key_by_fingerprint(
            tenant_id, secret_fingerprint
        )
        if existing is not None:
            if existing.name == name and existing.scopes == scopes:
                return existing
            updated = ApiKeyRecord(
                id=existing.id,
                tenant_id=tenant_id,
                name=name,
                secret_fingerprint=secret_fingerprint,
                scopes=scopes,
                created_at=existing.created_at,
                last_used_at=existing.last_used_at,
                revoked_at=existing.revoked_at,
                revoked_reason=existing.revoked_reason,
            )
            return self._repository.save_api_key_record(updated)
        return self._repository.save_api_key_record(
            ApiKeyRecord(
                tenant_id=tenant_id,
                name=name,
                secret_fingerprint=secret_fingerprint,
                scopes=scopes,
            )
        )

    def get_by_fingerprint(
        self, tenant_id: UUID, secret_fingerprint: str
    ) -> ApiKeyRecord | None:
        """Load metadata for one presented key fingerprint."""
        return self._repository.get_api_key_by_fingerprint(tenant_id, secret_fingerprint)

    def touch(self, tenant_id: UUID, secret_fingerprint: str) -> ApiKeyRecord | None:
        """Update last-used timestamp for a successfully authenticated key."""
        return self._repository.touch_api_key(
            tenant_id,
            secret_fingerprint,
            used_at=datetime.now(UTC),
        )

    def list_keys(self, tenant_id: UUID) -> tuple[ApiKeyRecord, ...]:
        """List key metadata for operator review."""
        return self._repository.list_api_keys(tenant_id)

    def revoke(
        self,
        tenant_id: UUID,
        key_id: UUID,
        *,
        reason: str = "",
    ) -> ApiKeyRecord:
        """Mark a key revoked without deleting audit evidence."""
        return self._repository.revoke_api_key(
            tenant_id,
            key_id,
            revoked_at=datetime.now(UTC),
            reason=reason,
        )

