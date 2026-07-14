"""Memory Gateway proposal service."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from memory_plane.contracts.dto import RetainCommand, RetainResult
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.audit import AuditEvent
from memory_plane.domain.models import MemoryItem, MemoryLayer, MemoryScope, Provenance
from memory_plane.domain.proposal import (
    MemoryProposal,
    MemoryProposalStatus,
    MemoryProposalTarget,
)
from memory_plane.services.privacy import PrivacyGuard
from memory_plane.services.retention import RetentionService


class MemoryProposalRepository(Protocol):
    """Storage boundary for proposed memory changes."""

    def append_proposal(
        self,
        proposal: MemoryProposal,
        idempotency_key: str | None = None,
        audit_event: AuditEvent | None = None,
    ) -> tuple[MemoryProposal, bool]:
        """Append a proposal or return the existing one for an idempotency key."""
        ...

    def get_proposal(self, tenant_id: UUID, proposal_id: UUID) -> MemoryProposal | None:
        """Load one proposal while enforcing tenant boundaries."""
        ...

    def list_proposals(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        namespace: str | None = None,
        status: MemoryProposalStatus | None = None,
        before_created_at: datetime | None = None,
        before_proposal_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[MemoryProposal, ...]:
        """List recent proposals for operator or curator review."""
        ...

    def save_proposal_review(
        self, proposal: MemoryProposal, audit_event: AuditEvent | None = None
    ) -> MemoryProposal:
        """Persist a proposal review/status update."""
        ...

    def accept_proposal_with_memory(
        self,
        proposal: MemoryProposal,
        item: MemoryItem,
        event: IntegrationEvent,
        *,
        reviewer: str,
        reason: str,
        idempotency_key: str,
        audit_event: AuditEvent | None = None,
    ) -> tuple[MemoryProposal, MemoryItem, bool]:
        """Atomically accept one proposal and append its canonical memory/event."""
        ...


class MemoryReasoner(Protocol):
    """Minimal LLM boundary used by Memory Gateway."""

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
class SubmitMemoryProposalCommand:
    """Request to submit a proposed memory change."""

    tenant_id: UUID
    workspace_id: UUID
    namespace: str
    requester: str
    proposal: str
    evidence: str = ""
    target: MemoryProposalTarget = MemoryProposalTarget.AUTO
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    confidence: float = 0.7
    importance: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitMemoryProposalResult:
    """Result of storing one proposal."""

    proposal: MemoryProposal
    created: bool


@dataclass(frozen=True, slots=True)
class ReviewMemoryProposalCommand:
    """Accept or reject a Memory Gateway proposal."""

    tenant_id: UUID
    proposal_id: UUID
    reviewer: str = "operator"
    reason: str = ""
    layer: MemoryLayer | None = None
    kind: str | None = None
    labels: tuple[str, ...] = ()
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewMemoryProposalResult:
    """Result of accepting/rejecting one proposal."""

    proposal: MemoryProposal
    retained: RetainResult | None = None


class MemoryProposalService:
    """Accept proposed memory changes without directly mutating durable memory."""

    def __init__(
        self,
        repository: MemoryProposalRepository,
        retention: RetentionService,
        privacy: PrivacyGuard | None = None,
        memory_llm: MemoryReasoner | None = None,
    ) -> None:
        """Bind proposals to storage and privacy policy."""
        self._repository = repository
        self._retention = retention
        self._privacy = privacy or PrivacyGuard.from_env()
        self._memory_llm = memory_llm

    def submit(
        self,
        command: SubmitMemoryProposalCommand,
        audit_event: AuditEvent | None = None,
    ) -> SubmitMemoryProposalResult:
        """Store a sanitized proposal for later curation/review."""
        proposal_text = self._privacy.apply(command.proposal)
        evidence_text = self._privacy.apply(command.evidence) if command.evidence else None
        metadata = dict(command.metadata)
        for decision in (proposal_text, evidence_text):
            if decision and decision.metadata:
                metadata = _merge_privacy_metadata(metadata, decision.metadata)
        target = command.target
        confidence = command.confidence
        importance = command.importance
        memory_llm = self._memory_llm
        if target == MemoryProposalTarget.AUTO and memory_llm is not None:
            (
                target,
                confidence,
                importance,
                metadata,
            ) = self._classify_proposal(
                proposal_text.text,
                evidence_text.text if evidence_text else "",
                target,
                confidence,
                importance,
                metadata,
                memory_llm,
            )
        proposal = MemoryProposal(
            tenant_id=command.tenant_id,
            workspace_id=command.workspace_id,
            agent_id=command.agent_id,
            thread_id=command.thread_id,
            namespace=command.namespace,
            requester=command.requester,
            target=target,
            proposal=proposal_text.text,
            evidence=evidence_text.text if evidence_text else "",
            confidence=confidence,
            importance=importance,
            metadata=metadata,
        )
        stored, created = self._repository.append_proposal(
            proposal,
            command.idempotency_key,
            audit_event=replace(
                audit_event,
                workspace_id=proposal.workspace_id,
                resource_id=str(proposal.id),
            )
            if audit_event is not None
            else None,
        )
        return SubmitMemoryProposalResult(proposal=stored, created=created)

    def _classify_proposal(
        self,
        proposal: str,
        evidence: str,
        target: MemoryProposalTarget,
        confidence: float,
        importance: float,
        metadata: dict[str, Any],
        memory_llm: MemoryReasoner,
    ) -> tuple[MemoryProposalTarget, float, float, dict[str, Any]]:
        try:
            payload = memory_llm.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are Memory Gateway for Obelisk Memory. "
                            "Classify a proposed memory update conservatively. "
                            "Return JSON only. target must be one of: fact, "
                            "preference, decision, task, graph, procedure. "
                            "Never accept or reject here; only classify."
                        ),
                    },
                    {
                        "role": "user",
                        "content": _trim_text(
                            "Classify this proposal. Return keys target, "
                            "confidence, importance, rationale.\n\n"
                            f"proposal: {proposal}\n\n"
                            f"evidence: {evidence}",
                            12000,
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=900,
            )
            if not isinstance(payload, dict):
                raise ValueError("memory proposal classifier returned a non-object JSON value")
        except Exception as exc:  # noqa: BLE001 - proposals must fail soft
            return (
                target,
                confidence,
                importance,
                {
                    **metadata,
                    "gateway_engine": "deterministic_fallback",
                    "gateway_llm_error": type(exc).__name__,
                },
            )

        classified_target = _parse_target(payload.get("target")) or target
        classified_confidence = _safe_float(payload.get("confidence"), confidence)
        classified_importance = _safe_float(payload.get("importance"), importance)
        return (
            classified_target,
            classified_confidence,
            classified_importance,
            {
                **metadata,
                "gateway_engine": "memory_llm",
                "gateway_version": "memory-proposal-classifier-llm-v1",
                "gateway_rationale": _trim_text(str(payload.get("rationale") or ""), 1000),
            },
        )

    def list(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        namespace: str | None = None,
        status: MemoryProposalStatus | None = None,
        before_created_at: datetime | None = None,
        before_proposal_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[MemoryProposal, ...]:
        """List proposals for review."""
        return self._repository.list_proposals(
            tenant_id,
            workspace_id,
            namespace=namespace,
            status=status,
            before_created_at=before_created_at,
            before_proposal_id=before_proposal_id,
            limit=limit,
        )

    def accept(
        self,
        command: ReviewMemoryProposalCommand,
        audit_event: AuditEvent | None = None,
    ) -> ReviewMemoryProposalResult:
        """Accept a proposal and create a recallable memory item."""
        proposal = self._load_for_review(command)
        if proposal.status == MemoryProposalStatus.REJECTED:
            raise ValueError("rejected proposal cannot be accepted")
        layer, kind = _memory_shape(proposal.target, command.layer, command.kind)
        labels = tuple(
            dict.fromkeys(
                (
                    "proposal",
                    proposal.namespace,
                    proposal.target.value,
                    *command.labels,
                )
            )
        )
        retain_command = RetainCommand(
                tenant_id=proposal.tenant_id,
                workspace_id=proposal.workspace_id,
                agent_id=proposal.agent_id,
                thread_id=proposal.thread_id,
                layer=layer,
                scope=MemoryScope.THREAD
                if proposal.thread_id is not None
                else MemoryScope.WORKSPACE,
                kind=kind,
                text=_accepted_memory_text(proposal),
                labels=labels,
                provenance=Provenance(
                    source_kind="memory_proposal",
                    origin_uri=f"proposal://{proposal.id}",
                    quote=_trim_text(proposal.evidence or proposal.proposal, 1800),
                    extraction_version="memory-proposal-review-v1",
                ),
                importance=proposal.importance,
                confidence=proposal.confidence,
                metadata={
                    "proposal_id": str(proposal.id),
                    "proposal_requester": proposal.requester,
                    "proposal_target": proposal.target.value,
                    "proposal_namespace": proposal.namespace,
                },
                idempotency_key=command.idempotency_key or f"accept-proposal:{proposal.id}",
        )
        item, event = self._retention.prepare(retain_command)
        atomic_accept = getattr(self._repository, "accept_proposal_with_memory", None)
        if callable(atomic_accept):
            if audit_event is not None:
                audit_event = replace(
                    audit_event,
                    workspace_id=proposal.workspace_id,
                    resource_id=str(proposal.id),
                )
            stored, memory, created = atomic_accept(
                proposal,
                item,
                event,
                reviewer=command.reviewer,
                reason=command.reason,
                idempotency_key=retain_command.idempotency_key or f"accept-proposal:{proposal.id}",
                audit_event=audit_event,
            )
            return ReviewMemoryProposalResult(
                proposal=stored,
                retained=RetainResult(
                    item=memory,
                    created=created,
                    queued_event_ids=(event.id,) if created else (),
                ),
            )
        retained = self._retention.retain(retain_command)
        reviewed = _reviewed_proposal(
            proposal,
            status=MemoryProposalStatus.ACCEPTED,
            reviewer=command.reviewer,
            reason=command.reason,
            metadata={**proposal.metadata, "accepted_memory_id": str(retained.item.id)},
        )
        stored = self._repository.save_proposal_review(reviewed)
        return ReviewMemoryProposalResult(proposal=stored, retained=retained)

    def auto_accept(self, proposal: MemoryProposal) -> ReviewMemoryProposalResult | None:
        """Accept only high-evidence, non-temporal operational claims automatically."""
        reason = _auto_accept_reason(proposal)
        if reason is None:
            return None
        return self.accept(
            ReviewMemoryProposalCommand(
                tenant_id=proposal.tenant_id,
                proposal_id=proposal.id,
                reviewer="obelisk-auto-policy",
                reason=reason,
                idempotency_key=f"auto-accept-proposal:{proposal.id}",
            )
        )

    def reject(
        self,
        command: ReviewMemoryProposalCommand,
        audit_event: AuditEvent | None = None,
    ) -> ReviewMemoryProposalResult:
        """Reject a proposal without creating durable memory."""
        proposal = self._load_for_review(command)
        if proposal.status == MemoryProposalStatus.ACCEPTED:
            raise ValueError("accepted proposal cannot be rejected")
        reviewed = _reviewed_proposal(
            proposal,
            status=MemoryProposalStatus.REJECTED,
            reviewer=command.reviewer,
            reason=command.reason,
        )
        if audit_event is not None:
            audit_event = replace(
                audit_event,
                workspace_id=proposal.workspace_id,
                resource_id=str(proposal.id),
            )
        stored = self._repository.save_proposal_review(reviewed, audit_event=audit_event)
        return ReviewMemoryProposalResult(proposal=stored)

    def _load_for_review(self, command: ReviewMemoryProposalCommand) -> MemoryProposal:
        proposal = self._repository.get_proposal(command.tenant_id, command.proposal_id)
        if proposal is None:
            raise KeyError("memory proposal not found")
        return proposal


def _reviewed_proposal(
    proposal: MemoryProposal,
    *,
    status: MemoryProposalStatus,
    reviewer: str,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> MemoryProposal:
    """Return an immutable reviewed copy."""
    return replace(
        proposal,
        status=status,
        reviewed_at=datetime.now(UTC),
        reviewer=reviewer.strip()[:120] or "operator",
        review_reason=reason.strip()[:1000],
        metadata=proposal.metadata if metadata is None else metadata,
    )


def _memory_shape(
    target: MemoryProposalTarget,
    layer: MemoryLayer | None,
    kind: str | None,
) -> tuple[MemoryLayer, str]:
    """Map proposal target to durable memory shape."""
    if layer is not None and kind:
        return layer, kind
    defaults = {
        MemoryProposalTarget.FACT: (MemoryLayer.SEMANTIC, "proposed_fact"),
        MemoryProposalTarget.PREFERENCE: (MemoryLayer.SOCIAL, "proposed_preference"),
        MemoryProposalTarget.DECISION: (MemoryLayer.CORE, "proposed_decision"),
        MemoryProposalTarget.TASK: (MemoryLayer.EPISODIC, "proposed_task"),
        MemoryProposalTarget.GRAPH: (MemoryLayer.SEMANTIC, "proposed_graph_fact"),
        MemoryProposalTarget.PROCEDURE: (MemoryLayer.PROCEDURAL, "proposed_procedure"),
        MemoryProposalTarget.AUTO: (MemoryLayer.SEMANTIC, "proposed_memory"),
    }
    default_layer, default_kind = defaults[target]
    return layer or default_layer, kind or default_kind


def _accepted_memory_text(proposal: MemoryProposal) -> str:
    """Build bounded durable memory text from a proposal and its evidence."""
    parts = [proposal.proposal.strip()]
    if proposal.evidence.strip():
        parts.extend(["", "Evidence:", proposal.evidence.strip()])
    return _trim_text("\n".join(parts), 6000)


def _merge_privacy_metadata(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Merge PrivacyGuard metadata without retaining raw sensitive values."""
    merged = dict(left)
    for key, value in right.items():
        if key == "privacy" and isinstance(value, dict):
            merged[key] = _merge_privacy_block(
                dict(merged.get(key, {})),
                value,
            )
            continue
        if isinstance(value, int):
            merged[key] = int(merged.get(key, 0)) + value
        elif isinstance(value, list):
            merged[key] = [*merged.get(key, []), *value]
        else:
            merged[key] = value
    return merged


def _merge_privacy_block(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Merge nested privacy audit counters."""
    counts = dict(left.get("counts") or {})
    for kind, count in dict(right.get("counts") or {}).items():
        counts[str(kind)] = int(counts.get(str(kind), 0)) + int(count)
    finding_kinds = sorted({*left.get("finding_kinds", []), *right.get("finding_kinds", [])})
    return {
        "action": right.get("action") or left.get("action"),
        "finding_count": int(left.get("finding_count") or 0) + int(right.get("finding_count") or 0),
        "finding_kinds": finding_kinds,
        "counts": counts,
    }


def _trim_text(text: str, limit: int) -> str:
    """Bound accepted proposal text."""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n..."


def _parse_target(value: Any) -> MemoryProposalTarget | None:
    try:
        target = MemoryProposalTarget(str(value or "").strip().lower())
    except ValueError:
        return None
    if target == MemoryProposalTarget.AUTO:
        return None
    return target


def _auto_accept_reason(proposal: MemoryProposal) -> str | None:
    """Return an audit reason only for evidence that is safe to automate."""
    allowed = {
        MemoryProposalTarget.PREFERENCE,
        MemoryProposalTarget.DECISION,
        MemoryProposalTarget.TASK,
        MemoryProposalTarget.PROCEDURE,
    }
    temporal_markers = (
        "раньше",
        "сейчас",
        "переш",
        "измен",
        "before",
        "formerly",
        "now ",
        "changed",
        "switched",
    )
    text = f"{proposal.proposal}\n{proposal.evidence}".lower()
    if proposal.target not in allowed:
        return None
    if proposal.confidence < 0.9 or not proposal.evidence.strip():
        return None
    if proposal.metadata.get("curator_engine") == "deterministic_fallback":
        return None
    has_temporal_marker = any(marker in text for marker in temporal_markers)
    if "source_turn_id:" not in proposal.evidence or has_temporal_marker:
        return None
    quotes = proposal.metadata.get("evidence_quotes")
    if not isinstance(quotes, list):
        return None
    verified_quotes = [
        str(quote).strip()
        for quote in quotes
        if len(str(quote).strip()) >= 8 and str(quote).strip() in proposal.evidence
    ]
    if not verified_quotes:
        return None
    return "auto-accepted: high-confidence claim with source-verified evidence quote"


def _safe_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(0.0, min(1.0, parsed))
