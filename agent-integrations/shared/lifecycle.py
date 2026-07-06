"""Runtime-agnostic lifecycle contract for native agent memory plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID


class AgentEventKind(StrEnum):
    """Agent lifecycle events worth turning into durable memory."""

    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    CHECKPOINT = "checkpoint"
    ERROR = "error"
    HUMAN_FEEDBACK = "human_feedback"
    RUN_SUMMARY = "run_summary"


@dataclass(frozen=True, slots=True)
class AgentRunContext:
    """Stable identity and scope passed by an agent runtime."""

    tenant_id: UUID
    workspace_id: UUID
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    operation: str = "agent_run"
    labels: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentLifecycleEvent:
    """Runtime-neutral event captured by a native plugin adapter."""

    kind: AgentEventKind
    text: str
    context: AgentRunContext
    tool_name: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryInjection:
    """Context material that a plugin should inject into an agent run."""

    markdown: str
    trace_ids: tuple[UUID, ...] = ()
    sources_used: tuple[str, ...] = ()


class AgentMemoryPlugin(Protocol):
    """Minimum contract implemented by OpenClaw/Hermes adapters."""

    def before_agent_run(self, context: AgentRunContext) -> MemoryInjection:
        """Recall and compile memory before the runtime starts reasoning."""
        ...

    def after_event(self, event: AgentLifecycleEvent) -> tuple[UUID, ...]:
        """Retain durable memory created from one runtime event."""
        ...

    def on_run_complete(self, context: AgentRunContext, summary: str) -> tuple[UUID, ...]:
        """Persist a run summary and trigger optional background maintenance."""
        ...
