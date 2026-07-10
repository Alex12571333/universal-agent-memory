"""Runtime-neutral implementation of the native memory plugin contract."""

from __future__ import annotations

import hashlib
from uuid import UUID

from shared.client import MemoryClient, MemoryServerClient
from shared.config import AgentMemoryConfig
from shared.lifecycle import (
    AgentEventKind,
    AgentLifecycleEvent,
    AgentMemoryPlugin,
    AgentRunContext,
    MemoryInjection,
)


class UniversalAgentMemoryPlugin(AgentMemoryPlugin):
    """Reusable memory plugin core used by OpenClaw/Hermes adapters."""

    def __init__(
        self,
        client: MemoryClient,
        config: AgentMemoryConfig,
    ) -> None:
        """Create a runtime-neutral plugin implementation."""
        self._client = client
        self._config = config

    def before_agent_run(self, context: AgentRunContext) -> MemoryInjection:
        """Recall and compile memory before an agent starts reasoning."""
        if not self._config.enabled:
            return MemoryInjection(markdown="")
        query = self._recall_query(context)
        data = self._client.recall(
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            agent_id=context.agent_id,
            thread_id=context.thread_id,
            labels=context.labels,
            query=query,
            operation=context.operation,
            top_k=self._config.recall_top_k,
            context_budget_tokens=self._config.context_budget_tokens,
        )
        context_block = data.get("context", {})
        trace_ids = tuple(UUID(str(item)) for item in context_block.get("trace_ids", ()))
        return MemoryInjection(
            markdown=str(context_block.get("markdown", "")),
            trace_ids=trace_ids,
            sources_used=tuple(str(item) for item in data.get("sources_used", ())),
        )

    def after_event(self, event: AgentLifecycleEvent) -> tuple[UUID, ...]:
        """Retain durable memory created from one runtime event."""
        if not self._config.enabled or not event.text.strip():
            return ()
        mapping = _event_mapping(event.kind)
        if mapping is None:
            return ()
        layer, kind = mapping
        if event.kind == AgentEventKind.MESSAGE and not self._config.retain_messages:
            return ()
        if event.kind == AgentEventKind.TOOL_CALL and not self._config.retain_tool_traces:
            return ()
        if event.kind == AgentEventKind.ERROR and not self._config.retain_errors:
            return ()
        text = _event_text(event)
        retained = self._client.retain(
            tenant_id=event.context.tenant_id,
            workspace_id=event.context.workspace_id,
            agent_id=event.context.agent_id,
            thread_id=event.context.thread_id,
            layer=layer,
            scope="thread" if event.context.thread_id else "workspace",
            kind=kind,
            text=text,
            labels=event.context.labels,
            idempotency_key=_event_idempotency_key(event),
        )
        return (retained.id,)

    def on_run_complete(self, context: AgentRunContext, summary: str) -> tuple[UUID, ...]:
        """Persist a run summary."""
        if not self._config.enabled or not summary.strip():
            return ()
        retained = self._client.retain(
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            agent_id=context.agent_id,
            thread_id=context.thread_id,
            layer="episodic",
            scope="thread" if context.thread_id else "workspace",
            kind="run_summary",
            text=summary,
            labels=context.labels,
            idempotency_key=(
                f"run-summary:{context.thread_id or context.workspace_id}:"
                f"{_stable_digest(summary)}"
            ),
        )
        return (retained.id,)

    def save_checkpoint(self, context: AgentRunContext, state: dict[str, object]) -> UUID | None:
        """Persist working state for runtimes that expose checkpoint hooks."""
        if not self._config.enabled or context.thread_id is None:
            return None
        return self._client.save_checkpoint(
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            thread_id=context.thread_id,
            state=dict(state),
        )

    def _recall_query(self, context: AgentRunContext) -> str:
        labels = ", ".join(context.labels) if context.labels else "none"
        return (
            f"Recall memory for {self._config.integration_name} operation "
            f"{context.operation}. Labels: {labels}."
        )


def build_plugin(config: AgentMemoryConfig | None = None) -> UniversalAgentMemoryPlugin:
    """Create a plugin from env/config for runtime-specific adapters."""
    cfg = config or AgentMemoryConfig.from_env()
    return UniversalAgentMemoryPlugin(MemoryServerClient(cfg), cfg)


def _event_mapping(kind: AgentEventKind) -> tuple[str, str] | None:
    if kind == AgentEventKind.MESSAGE:
        return ("episodic", "agent_message")
    if kind == AgentEventKind.TOOL_CALL:
        return ("procedural", "tool_trace")
    if kind == AgentEventKind.ERROR:
        return ("error", "agent_error")
    if kind == AgentEventKind.HUMAN_FEEDBACK:
        return ("social", "human_feedback")
    if kind == AgentEventKind.RUN_SUMMARY:
        return ("episodic", "run_summary")
    if kind == AgentEventKind.CHECKPOINT:
        return ("working", "checkpoint_note")
    return None


def _event_text(event: AgentLifecycleEvent) -> str:
    if event.kind == AgentEventKind.TOOL_CALL and event.tool_name:
        return f"Tool `{event.tool_name}`: {event.text}"
    if event.kind == AgentEventKind.ERROR and event.error:
        return f"{event.text}\n\nError: {event.error}"
    return event.text


def _event_idempotency_key(event: AgentLifecycleEvent) -> str:
    thread = event.context.thread_id or event.context.workspace_id
    tool = event.tool_name or "none"
    return f"agent-event:{thread}:{event.kind.value}:{tool}:{_stable_digest(event.text)}"


def _stable_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
