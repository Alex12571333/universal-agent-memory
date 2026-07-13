"""Small, deterministic workspace orientation for opt-in agent startup."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from memory_plane.domain.models import MemoryLayer, MemoryScope, MemoryStatus
from memory_plane.ports.repositories import MemoryLedger
from memory_plane.services.context import ContextCompiler


@dataclass(frozen=True, slots=True)
class SessionSeed:
    """A bounded orientation block; it is never a substitute for recall."""

    budget_tokens: int
    used_tokens: int
    trace_ids: tuple[UUID, ...]
    markdown: str


class SessionSeedService:
    """Build a small stable inventory from recallable, shared workspace heads."""

    _LAYERS = (MemoryLayer.CORE, MemoryLayer.WORKING, MemoryLayer.PROCEDURAL)
    _VISIBLE_SCOPES = {
        MemoryScope.WORKSPACE,
        MemoryScope.TEAM,
        MemoryScope.ORGANIZATION,
    }

    def __init__(self, ledger: MemoryLedger) -> None:
        self._ledger = ledger

    def build(
        self, tenant_id: UUID, workspace_id: UUID, *, budget_tokens: int = 512
    ) -> SessionSeed:
        """Return an intentionally small shared-memory orientation package."""
        budget = max(128, min(budget_tokens, 4096))
        rows = self._ledger.list_for_workspace(
            tenant_id, workspace_id, layers=self._LAYERS
        )
        candidates = [
            item
            for item in rows
            if item.scope in self._VISIBLE_SCOPES
            and item.status in {MemoryStatus.ACTIVE, MemoryStatus.PINNED}
            and self._ledger.is_recallable_head(tenant_id, item.id)
        ]
        candidates.sort(
            key=lambda item: (
                item.status == MemoryStatus.PINNED,
                item.importance,
                item.created_at,
            ),
            reverse=True,
        )
        selected = []
        used = 0
        for item in candidates[:64]:
            cost = ContextCompiler.estimate_tokens(item.text)
            if used + cost > budget:
                continue
            selected.append(item)
            used += cost
        markdown = "\n".join(f"- {item.text}" for item in selected)
        return SessionSeed(
            budget_tokens=budget,
            used_tokens=used,
            trace_ids=tuple(item.id for item in selected),
            markdown=markdown,
        )
