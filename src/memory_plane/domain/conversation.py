"""Append-only raw conversation ledger domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

PURGED_CONVERSATION_CONTENT = "[PURGED_AFTER_CURATION]"


class ConversationRetentionPolicy(StrEnum):
    """How a runtime wants a raw turn to be treated by maintenance workers."""

    RAW_ONLY = "raw_only"
    CURATED_ONLY = "curated_only"
    RAW_AND_CURATED = "raw_and_curated"


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """One message inside an immutable conversation turn."""

    role: str
    content: str
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject empty or malformed transcript entries."""
        if not self.role.strip():
            raise ValueError("conversation message role must not be empty")
        if not self.content.strip():
            raise ValueError("conversation message content must not be empty")


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """Immutable raw transcript unit separate from curated durable memory."""

    tenant_id: UUID
    workspace_id: UUID
    thread_id: UUID
    messages: tuple[ConversationMessage, ...]
    id: UUID = field(default_factory=uuid4)
    namespace: str = "default"
    agent_id: UUID | None = None
    source_kind: str = "api"
    retention_policy: ConversationRetentionPolicy = (
        ConversationRetentionPolicy.RAW_AND_CURATED
    )
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Validate transcript boundaries."""
        if not self.namespace.strip():
            raise ValueError("conversation namespace must not be empty")
        if not self.source_kind.strip():
            raise ValueError("conversation source_kind must not be empty")
        if not self.messages:
            raise ValueError("conversation turn requires at least one message")
