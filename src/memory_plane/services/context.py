"""Selective context delivery: compile memory into an operation-specific package."""

from __future__ import annotations

from collections import defaultdict

from memory_plane.contracts.dto import ContextRecipe, RecallResult
from memory_plane.domain.models import ContextPackage, ContextSection, MemoryItem, MemoryLayer


class ContextCompiler:
    """Select and order memories without exceeding the operation token budget."""

    def compile(self, recall: RecallResult, recipe: ContextRecipe) -> ContextPackage:
        """Compile ranked candidates into ordered sections with trace IDs.

        Core/working layers are considered first; other layers follow
        `recipe.layer_order`. Token estimation is deterministic and deliberately
        conservative, so an adapter can later replace it with a model tokenizer.
        """
        by_layer: dict[MemoryLayer, list[MemoryItem]] = defaultdict(list)
        for candidate in recall.candidates:
            by_layer[candidate.item.layer].append(candidate.item)

        order = tuple(dict.fromkeys((*recipe.always_include, *recipe.layer_order)))
        sections: list[ContextSection] = []
        used = 0
        selected_ids: set[object] = set()

        for layer in order:
            accepted: list[MemoryItem] = []
            limit = recipe.per_layer_limit.get(layer, 100)
            for item in by_layer.get(layer, ()):
                if item.id in selected_ids or len(accepted) >= limit:
                    continue
                cost = self.estimate_tokens(item.text)
                if used + cost > recipe.budget_tokens:
                    continue
                accepted.append(item)
                selected_ids.add(item.id)
                used += cost
            if accepted:
                sections.append(
                    ContextSection(
                        name=layer.value,
                        items=tuple(accepted),
                        estimated_tokens=sum(self.estimate_tokens(x.text) for x in accepted),
                    )
                )

        trace_ids = tuple(item.id for section in sections for item in section.items)
        return ContextPackage(
            operation=recipe.operation,
            sections=tuple(sections),
            budget_tokens=recipe.budget_tokens,
            used_tokens=used,
            trace_ids=trace_ids,
        )

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Estimate tokens using a portable characters/4 heuristic."""
        return max(1, (len(text) + 3) // 4)
