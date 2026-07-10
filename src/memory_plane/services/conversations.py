"""Raw conversation ledger service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from memory_plane.contracts.dto import RetainCommand, RetainResult
from memory_plane.domain.conversation import (
    ConversationMessage,
    ConversationRetentionPolicy,
    ConversationTurn,
)
from memory_plane.domain.models import MemoryLayer, MemoryScope, Provenance
from memory_plane.services.privacy import PrivacyGuard
from memory_plane.services.proposals import (
    MemoryProposalService,
    SubmitMemoryProposalCommand,
    SubmitMemoryProposalResult,
)
from memory_plane.services.retention import RetentionService


class ConversationLedger(Protocol):
    """Storage boundary for immutable transcript turns."""

    def append_turn(
        self, turn: ConversationTurn, idempotency_key: str | None = None
    ) -> tuple[ConversationTurn, bool]:
        """Append a raw turn or return the existing turn for an idempotency key."""
        ...

    def get_turn(self, tenant_id: UUID, turn_id: UUID) -> ConversationTurn | None:
        """Load one transcript turn while enforcing tenant boundaries."""
        ...

    def list_turns(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        thread_id: UUID | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> tuple[ConversationTurn, ...]:
        """List recent transcript turns under tenant/workspace boundaries."""
        ...

    def purge_turn_content(self, tenant_id: UUID, turn_id: UUID) -> bool:
        """Irreversibly replace raw message content while retaining audit identity."""
        ...

    def purge_expired_turns(
        self, tenant_id: UUID, workspace_id: UUID, *, now: datetime, limit: int
    ) -> tuple[UUID, ...]:
        """Purge expired staged raw transcripts and return their stable IDs."""
        ...


class MemoryReasoner(Protocol):
    """Minimal LLM boundary used by the curator without provider coupling."""

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = 0.0,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Return a JSON object from a memory reasoning model."""
        ...


@dataclass(frozen=True, slots=True)
class AppendConversationTurnCommand:
    """Request to append one raw conversation turn."""

    tenant_id: UUID
    workspace_id: UUID
    thread_id: UUID
    messages: tuple[ConversationMessage, ...]
    namespace: str = "default"
    agent_id: UUID | None = None
    source_kind: str = "api"
    retention_policy: ConversationRetentionPolicy = ConversationRetentionPolicy.RAW_AND_CURATED
    metadata: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class AppendConversationTurnResult:
    """Result of appending a raw transcript turn."""

    turn: ConversationTurn
    created: bool


@dataclass(frozen=True, slots=True)
class CurateConversationTurnCommand:
    """Request to distill one raw turn into curated memory."""

    tenant_id: UUID
    turn_id: UUID
    layer: MemoryLayer = MemoryLayer.EPISODIC
    kind: str = "conversation_summary"
    labels: tuple[str, ...] = ()
    importance: float = 0.4
    confidence: float = 0.65
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class CurateConversationTurnResult:
    """Curation output, proposal-first when a review gateway is configured."""

    retained: RetainResult | None = None
    proposal: SubmitMemoryProposalResult | None = None


class ConversationService:
    """Validate and append raw transcript turns without making them prompt context."""

    def __init__(
        self,
        ledger: ConversationLedger,
        privacy: PrivacyGuard | None = None,
        curated_only_ttl_seconds: int | None = None,
    ) -> None:
        """Bind service to a ledger and privacy policy."""
        self._ledger = ledger
        self._privacy = privacy or PrivacyGuard.from_env()
        configured_ttl = curated_only_ttl_seconds
        if configured_ttl is None:
            configured_ttl = int(os.getenv("UAM_CONVERSATION_CURATED_ONLY_TTL_SECONDS", "86400"))
        if not 300 <= configured_ttl <= 604800:
            raise ValueError(
                "UAM_CONVERSATION_CURATED_ONLY_TTL_SECONDS must be between 300 and 604800"
            )
        self._curated_only_ttl_seconds = configured_ttl

    def append_turn(self, command: AppendConversationTurnCommand) -> AppendConversationTurnResult:
        """Append a redacted raw turn to the immutable conversation ledger."""
        messages = []
        redaction_metadata: dict[str, Any] = {}
        for message in command.messages:
            decision = self._privacy.apply(message.content)
            if decision.metadata:
                redaction_metadata = _merge_privacy_metadata(redaction_metadata, decision.metadata)
            messages.append(
                ConversationMessage(
                    role=message.role,
                    name=message.name,
                    content=decision.text,
                    metadata={**message.metadata, **decision.metadata},
                )
            )
        created_at = datetime.now(UTC)
        expires_at = (
            created_at + timedelta(seconds=self._curated_only_ttl_seconds)
            if command.retention_policy == ConversationRetentionPolicy.CURATED_ONLY
            else None
        )
        turn = ConversationTurn(
            tenant_id=command.tenant_id,
            workspace_id=command.workspace_id,
            thread_id=command.thread_id,
            namespace=command.namespace,
            agent_id=command.agent_id,
            source_kind=command.source_kind,
            retention_policy=command.retention_policy,
            messages=tuple(messages),
            metadata={**command.metadata, **redaction_metadata},
            created_at=created_at,
            expires_at=expires_at,
        )
        stored, created = self._ledger.append_turn(turn, command.idempotency_key)
        return AppendConversationTurnResult(turn=stored, created=created)

    def list_turns(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        thread_id: UUID | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> tuple[ConversationTurn, ...]:
        """List recent raw transcript turns for operator review or reprocessing."""
        return self._ledger.list_turns(
            tenant_id,
            workspace_id,
            thread_id=thread_id,
            namespace=namespace,
            limit=limit,
        )

    def purge_expired_turns(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        limit: int = 500,
        now: datetime | None = None,
    ) -> tuple[UUID, ...]:
        """Purge due staged transcripts through an operator/scheduled maintenance path."""
        return self._ledger.purge_expired_turns(
            tenant_id,
            workspace_id,
            now=now or datetime.now(UTC),
            limit=max(1, min(int(limit), 5000)),
        )


class ConversationCurator:
    """Deterministically turn raw transcript turns into curated memory items."""

    def __init__(
        self,
        ledger: ConversationLedger,
        retention: RetentionService,
        memory_llm: MemoryReasoner | None = None,
        proposals: MemoryProposalService | None = None,
    ) -> None:
        """Bind raw transcript reads to the canonical memory write path."""
        self._ledger = ledger
        self._retention = retention
        self._memory_llm = memory_llm
        self._proposals = proposals

    def curate_turn(self, command: CurateConversationTurnCommand) -> CurateConversationTurnResult:
        """Create a reviewable proposal, or legacy durable memory if no gateway exists."""
        turn = self._ledger.get_turn(command.tenant_id, command.turn_id)
        if turn is None:
            raise KeyError("conversation turn not found")
        if turn.retention_policy == ConversationRetentionPolicy.RAW_ONLY:
            raise ValueError("conversation turn retention policy is raw_only")
        text, llm_metadata = self._summary_text(turn)
        if self._proposals is not None:
            proposal = self._proposals.submit(
                SubmitMemoryProposalCommand(
                    tenant_id=turn.tenant_id,
                    workspace_id=turn.workspace_id,
                    agent_id=turn.agent_id,
                    thread_id=turn.thread_id,
                    namespace=turn.namespace,
                    requester="conversation-curator",
                    proposal=text,
                    evidence=_trim_text(_conversation_text(turn), 6000),
                    confidence=command.confidence,
                    importance=command.importance,
                    metadata={
                        **llm_metadata,
                        "source_turn_id": str(turn.id),
                        "curation_boundary": "proposal_required",
                    },
                    idempotency_key=(
                        command.idempotency_key or f"curate-conversation-turn:{turn.id}"
                    ),
                )
            )
            if turn.retention_policy == ConversationRetentionPolicy.CURATED_ONLY:
                if not self._ledger.purge_turn_content(turn.tenant_id, turn.id):
                    raise RuntimeError("curation proposal was saved but raw content purge failed")
            return CurateConversationTurnResult(proposal=proposal)
        labels = tuple(
            dict.fromkeys(
                (
                    "conversation",
                    "curated",
                    turn.namespace,
                    *command.labels,
                )
            )
        )
        result = self._retention.retain(
            RetainCommand(
                tenant_id=turn.tenant_id,
                workspace_id=turn.workspace_id,
                agent_id=turn.agent_id,
                thread_id=turn.thread_id,
                layer=command.layer,
                scope=MemoryScope.THREAD,
                kind=command.kind,
                text=text,
                labels=labels,
                provenance=Provenance(
                    source_kind="conversation_ledger",
                    origin_uri=f"conversation://{turn.id}",
                    quote=_trim_text(text, 1800),
                    extraction_version="conversation-curator-v1",
                ),
                importance=command.importance,
                confidence=command.confidence,
                metadata=llm_metadata,
                idempotency_key=(command.idempotency_key or f"curate-conversation-turn:{turn.id}"),
            )
        )
        if turn.retention_policy == ConversationRetentionPolicy.CURATED_ONLY:
            if not self._ledger.purge_turn_content(turn.tenant_id, turn.id):
                raise RuntimeError("curated memory was saved but raw content purge failed")
        return CurateConversationTurnResult(retained=result)

    def _summary_text(self, turn: ConversationTurn) -> tuple[str, dict[str, Any]]:
        memory_llm = self._memory_llm
        if memory_llm is not None:
            llm_result = self._llm_summary_text(turn, memory_llm)
            if llm_result is not None:
                return llm_result
        return self._deterministic_summary_text(turn), {
            "curator_engine": "deterministic",
            "curator_version": "conversation-curator-v1",
        }

    def _llm_summary_text(
        self,
        turn: ConversationTurn,
        memory_llm: MemoryReasoner,
    ) -> tuple[str, dict[str, Any]] | None:
        try:
            payload = memory_llm.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are Memory Curator for Obelisk Memory. "
                            "Extract only durable, evidence-backed information. "
                            "Do not invent facts. Return JSON object only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": _trim_text(
                            "Curate this raw conversation turn into compact "
                            "memory. Return keys summary, durable_facts, "
                            "decisions, preferences, open_tasks, confidence.\n\n"
                            f"{_conversation_text(turn)}",
                            24000,
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=1800,
            )
        except Exception as exc:  # noqa: BLE001 - fail-soft memory maintenance
            return (
                self._deterministic_summary_text(turn),
                {
                    "curator_engine": "deterministic_fallback",
                    "curator_version": "conversation-curator-v1",
                    "llm_error": type(exc).__name__,
                },
            )

        text = _curation_payload_to_text(payload)
        if not text:
            return None
        return text, {
            "curator_engine": "memory_llm",
            "curator_version": "conversation-curator-llm-v1",
            "llm_confidence": _safe_float(payload.get("confidence")),
        }

    @staticmethod
    def _deterministic_summary_text(turn: ConversationTurn) -> str:
        lines = [
            "Conversation turn summary",
            f"Namespace: {turn.namespace}",
            f"Source: {turn.source_kind}",
            f"Turn: {turn.id}",
            "",
            "Messages:",
        ]
        for message in turn.messages:
            name = f" ({message.name})" if message.name else ""
            content = _trim_text(message.content.replace("\n", " "), 900)
            lines.append(f"- {message.role}{name}: {content}")
        return _trim_text("\n".join(lines), 6000)


def _merge_privacy_metadata(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Merge PrivacyGuard metadata without retaining raw secret values."""
    merged = dict(left)
    for key, value in right.items():
        if isinstance(value, int):
            merged[key] = int(merged.get(key, 0)) + value
        elif isinstance(value, list):
            merged[key] = [*merged.get(key, []), *value]
        else:
            merged[key] = value
    return merged


def _trim_text(text: str, limit: int) -> str:
    """Bound deterministic curation output."""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n..."


def _conversation_text(turn: ConversationTurn) -> str:
    lines = [
        f"turn_id: {turn.id}",
        f"namespace: {turn.namespace}",
        f"source_kind: {turn.source_kind}",
        "messages:",
    ]
    for message in turn.messages:
        name = f" ({message.name})" if message.name else ""
        lines.append(f"- {message.role}{name}: {message.content}")
    return "\n".join(lines)


def _curation_payload_to_text(payload: dict[str, Any]) -> str:
    lines = ["Conversation memory curation"]
    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines.extend(["", "Summary:", summary])
    sections = (
        ("durable_facts", "Durable facts"),
        ("decisions", "Decisions"),
        ("preferences", "Preferences"),
        ("open_tasks", "Open tasks"),
    )
    for key, title in sections:
        values = _string_list(payload.get(key))
        if values:
            lines.extend(["", f"{title}:"])
            lines.extend(f"- {value}" for value in values)
    text = "\n".join(lines).strip()
    if text == "Conversation memory curation":
        return ""
    return _trim_text(text, 6000)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, parsed))
