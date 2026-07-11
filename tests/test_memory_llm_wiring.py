from __future__ import annotations

from uuid import uuid4

from memory_plane.adapters.in_memory import InMemoryMemoryStore
from memory_plane.domain.conversation import ConversationMessage
from memory_plane.domain.proposal import MemoryProposalTarget
from memory_plane.services.conversations import (
    AppendConversationTurnCommand,
    ConversationCurator,
    ConversationService,
    CurateConversationTurnCommand,
)
from memory_plane.services.proposals import (
    MemoryProposalService,
    SubmitMemoryProposalCommand,
)
from memory_plane.services.retention import RetentionService


class FakeReasoner:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat_json(self, messages, *, temperature=0.0, max_tokens=None):
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return self.payload


class FailingReasoner:
    def chat_json(self, messages, *, temperature=0.0, max_tokens=None):
        raise RuntimeError("llm offline")


def test_conversation_curator_uses_memory_llm_when_available() -> None:
    store = InMemoryMemoryStore()
    tenant_id = uuid4()
    workspace_id = uuid4()
    thread_id = uuid4()
    turn = ConversationService(store).append_turn(
        AppendConversationTurnCommand(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
            namespace="openclaw",
            messages=(
                ConversationMessage(
                    role="user",
                    content="Пользователь хочет русский премиальный интерфейс.",
                ),
            ),
        )
    )
    reasoner = FakeReasoner(
        {
            "summary": "Пользователь явно просит премиальный русский интерфейс.",
            "preferences": ["Интерфейс должен быть на русском"],
            "durable_facts": [],
            "decisions": [],
            "open_tasks": ["Довести UI до premium polish"],
            "confidence": 0.92,
        }
    )

    result = ConversationCurator(
        store,
        RetentionService(store),
        memory_llm=reasoner,
        proposals=MemoryProposalService(store, RetentionService(store)),
    ).curate_turn(
        CurateConversationTurnCommand(
            tenant_id=tenant_id,
            turn_id=turn.turn.id,
        )
    )

    assert result.proposal is not None
    proposal = result.proposal.proposal
    assert proposal.metadata["curator_engine"] == "memory_llm"
    assert proposal.metadata["llm_confidence"] == 0.92
    assert proposal.metadata["claim_status"] == "unverified"
    assert proposal.metadata["source_observed_at"] == turn.turn.created_at.isoformat()
    assert "Пользователь явно просит" in proposal.proposal
    assert "Интерфейс должен быть на русском" in proposal.proposal
    assert f"source_turn_id: {turn.turn.id}" in proposal.evidence
    assert reasoner.calls[0]["max_tokens"] == 900


def test_conversation_curator_chunks_long_transcript_then_reduces() -> None:
    store = InMemoryMemoryStore()
    tenant_id, workspace_id, thread_id = uuid4(), uuid4(), uuid4()
    turn = ConversationService(store).append_turn(
        AppendConversationTurnCommand(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
            messages=(ConversationMessage(role="user", content="x" * 21_000),),
        )
    )
    reasoner = FakeReasoner({"summary": "bounded result", "confidence": 0.8})

    result = ConversationCurator(
        store,
        RetentionService(store),
        memory_llm=reasoner,
        proposals=MemoryProposalService(store, RetentionService(store)),
    ).curate_turn(CurateConversationTurnCommand(tenant_id=tenant_id, turn_id=turn.turn.id))

    assert result.proposal is not None
    assert len(reasoner.calls) == 4  # three chunks plus a bounded reducer call
    assert [call["max_tokens"] for call in reasoner.calls] == [900, 900, 900, 1800]
    assert "x" * 10_001 not in reasoner.calls[-1]["messages"][1]["content"]


def test_conversation_curator_falls_back_when_memory_llm_fails() -> None:
    store = InMemoryMemoryStore()
    tenant_id = uuid4()
    workspace_id = uuid4()
    thread_id = uuid4()
    turn = ConversationService(store).append_turn(
        AppendConversationTurnCommand(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
            messages=(ConversationMessage(role="user", content="Надо fail-soft"),),
        )
    )

    result = ConversationCurator(
        store,
        RetentionService(store),
        memory_llm=FailingReasoner(),
        proposals=MemoryProposalService(store, RetentionService(store)),
    ).curate_turn(
        CurateConversationTurnCommand(
            tenant_id=tenant_id,
            turn_id=turn.turn.id,
        )
    )

    assert result.proposal is not None
    proposal = result.proposal.proposal
    assert proposal.metadata["curator_engine"] == "deterministic_fallback"
    assert proposal.metadata["llm_error"] == "RuntimeError"
    assert "Conversation turn summary" in proposal.proposal


def test_conversation_curator_rejects_missing_proposal_boundary() -> None:
    store = InMemoryMemoryStore()

    try:
        ConversationCurator(store, RetentionService(store))
    except ValueError as error:
        assert "requires MemoryProposalService" in str(error)
    else:
        raise AssertionError("curator must reject a direct durable-memory fallback")


def test_auto_policy_accepts_only_high_confidence_evidence_linked_preference() -> None:
    store = InMemoryMemoryStore()
    service = MemoryProposalService(store, RetentionService(store))
    submitted = service.submit(
        SubmitMemoryProposalCommand(
            tenant_id=uuid4(),
            workspace_id=uuid4(),
            namespace="openclaw",
            requester="conversation-curator",
            target=MemoryProposalTarget.PREFERENCE,
            proposal="Пользователь предпочитает краткие ответы.",
            evidence=(
                "source_turn_id: 00000000-0000-0000-0000-000000000001\n"
                "- user: Предпочитаю кратко."
            ),
            confidence=0.95,
            metadata={
                "curator_engine": "memory_llm",
                "evidence_quotes": ["Предпочитаю кратко."],
            },
        )
    )

    accepted = service.auto_accept(submitted.proposal)

    assert accepted is not None
    assert accepted.proposal.status.value == "accepted"
    assert accepted.proposal.reviewer == "obelisk-auto-policy"


def test_auto_policy_rejects_llm_claim_without_source_verified_quote() -> None:
    store = InMemoryMemoryStore()
    service = MemoryProposalService(store, RetentionService(store))
    submitted = service.submit(
        SubmitMemoryProposalCommand(
            tenant_id=uuid4(),
            workspace_id=uuid4(),
            namespace="openclaw",
            requester="conversation-curator",
            target=MemoryProposalTarget.PREFERENCE,
            proposal="Пользователь предпочитает краткие ответы.",
            evidence=(
                "source_turn_id: 00000000-0000-0000-0000-000000000001\n"
                "- user: Предпочитаю кратко."
            ),
            confidence=0.95,
            metadata={"curator_engine": "memory_llm"},
        )
    )

    assert service.auto_accept(submitted.proposal) is None


def test_memory_gateway_classifies_auto_proposal_with_memory_llm() -> None:
    store = InMemoryMemoryStore()
    reasoner = FakeReasoner(
        {
            "target": "preference",
            "confidence": 0.88,
            "importance": 0.76,
            "rationale": "Explicit user preference.",
        }
    )
    service = MemoryProposalService(
        store,
        RetentionService(store),
        memory_llm=reasoner,
    )

    result = service.submit(
        SubmitMemoryProposalCommand(
            tenant_id=uuid4(),
            workspace_id=uuid4(),
            namespace="hermes",
            requester="test",
            target=MemoryProposalTarget.AUTO,
            proposal="User prefers short Russian bullet-point answers.",
            evidence="User explicitly asked for concise Russian bullet points.",
        )
    )

    assert result.proposal.target == MemoryProposalTarget.PREFERENCE
    assert result.proposal.confidence == 0.88
    assert result.proposal.importance == 0.76
    assert result.proposal.metadata["gateway_engine"] == "memory_llm"
    assert "Explicit user preference" in result.proposal.metadata["gateway_rationale"]
