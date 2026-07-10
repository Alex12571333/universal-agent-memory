"""Transport models shared by synchronous Python client operations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RetainRequest:
    text: str
    layer: str = "semantic"
    scope: str = "workspace"
    kind: str = "fact"
    source_kind: str = "sdk-python"
    agent_id: str | None = None
    thread_id: str | None = None
    labels: tuple[str, ...] = ()
    importance: float = 0.5
    confidence: float = 0.7
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True, slots=True)
class RetainResponse:
    id: str
    created: bool
    queued_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecallRequest:
    query: str
    agent_id: str | None = None
    thread_id: str | None = None
    layers: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    top_k: int = 12
    minimum_score: float = 0.0
    operation: str = "chat_reply"
    context_budget_tokens: int = 4000

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True, slots=True)
class MemoryResult:
    id: str
    text: str
    layer: str
    score: float
    source: str


@dataclass(frozen=True, slots=True)
class CompiledContext:
    operation: str
    used_tokens: int
    budget_tokens: int
    markdown: str
    trace_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecallResponse:
    results: tuple[MemoryResult, ...]
    sources_used: tuple[str, ...]
    context: CompiledContext


@dataclass(frozen=True, slots=True)
class IngestTextRequest:
    text: str
    origin_uri: str
    agent_id: str | None = None
    thread_id: str | None = None
    labels: tuple[str, ...] = ()
    chunk_size_chars: int = 2400
    chunk_overlap_chars: int = 240

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True, slots=True)
class IngestTextResponse:
    document_checksum: str
    memory_ids: tuple[str, ...]
    created_count: int


@dataclass(frozen=True, slots=True)
class IdentityProvisionRequest:
    agent_id: str
    agent_name: str
    agent_role: str
    tenant_id: str | None = None
    workspace_id: str | None = None
    agent_config: dict[str, Any] = field(default_factory=dict)
    thread_id: str | None = None
    thread_status: str = "active"

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True, slots=True)
class IdentityProvisionResponse:
    agent: dict[str, Any]
    thread: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay_seconds: float = 0.1
    retry_statuses: frozenset[int] = field(
        default_factory=lambda: frozenset({429, 502, 503, 504})
    )

    def __post_init__(self) -> None:
        if self.max_retries < 0 or self.base_delay_seconds < 0:
            raise ValueError("retry values must not be negative")


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: list(item) if isinstance(item, tuple) else item
        for key, item in value.items()
        if item is not None
    }
