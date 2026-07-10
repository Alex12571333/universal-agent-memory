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
    ).curate_turn(
        CurateConversationTurnCommand(
            tenant_id=tenant_id,
            turn_id=turn.turn.id,
        )
    )

    assert result.retained is not None
    retained = result.retained
    assert retained.item.metadata["curator_engine"] == "memory_llm"
    assert retained.item.metadata["llm_confidence"] == 0.92
    assert "Пользователь явно просит" in retained.item.text
    assert "Интерфейс должен быть на русском" in retained.item.text
    assert reasoner.calls[0]["max_tokens"] == 1800


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
    ).curate_turn(
        CurateConversationTurnCommand(
            tenant_id=tenant_id,
            turn_id=turn.turn.id,
        )
    )

    assert result.retained is not None
    retained = result.retained
    assert retained.item.metadata["curator_engine"] == "deterministic_fallback"
    assert retained.item.metadata["llm_error"] == "RuntimeError"
    assert "Conversation turn summary" in retained.item.text


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
