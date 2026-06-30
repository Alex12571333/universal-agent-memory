from __future__ import annotations

import unittest
from uuid import uuid4

from memory_plane.bootstrap import build_in_memory_container
from memory_plane.contracts.dto import (
    ContextRecipe,
    IngestDocumentCommand,
    RecallQuery,
    RetainCommand,
)
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.models import MemoryLayer, MemoryScope, Provenance
from memory_plane.workers.handlers import RetainedEventRouter


class MemoryPlaneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.container = build_in_memory_container()
        self.tenant = uuid4()
        self.workspace = uuid4()
        self.agent = uuid4()

    def retain(
        self,
        text: str,
        *,
        layer: MemoryLayer = MemoryLayer.SEMANTIC,
        key: str | None = None,
        tenant=None,
    ):
        return self.container.retention.retain(
            RetainCommand(
                tenant_id=tenant or self.tenant,
                workspace_id=self.workspace,
                agent_id=self.agent,
                layer=layer,
                scope=MemoryScope.WORKSPACE,
                kind="fact",
                text=text,
                provenance=Provenance(source_kind="test"),
                idempotency_key=key,
            )
        )

    def test_retain_is_idempotent_and_emits_once(self) -> None:
        first = self.retain("Alpha release is July 15", key="turn-1")
        second = self.retain("different retry body", key="turn-1")

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.item.id, second.item.id)
        self.assertEqual(1, len(self.container.store.events))

    def test_recall_enforces_tenant_and_ranks_lexical_match(self) -> None:
        expected = self.retain("Ivan owns the Alpha release")
        self.retain("Unrelated private tenant fact", tenant=uuid4())

        result = self.container.retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="Who owns Alpha release?",
            )
        )

        self.assertEqual(expected.item.id, result.candidates[0].item.id)
        self.assertTrue(all(row.item.tenant_id == self.tenant for row in result.candidates))

    def test_context_compiler_honors_budget_and_layer_priority(self) -> None:
        self.retain("Always obey workspace policy", layer=MemoryLayer.CORE)
        self.retain("Alpha release fact")
        recall = self.container.retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="Alpha workspace policy",
            )
        )
        package = self.container.context.compile(
            recall,
            ContextRecipe(
                operation="planner",
                budget_tokens=128,
                layer_order=(MemoryLayer.SEMANTIC,),
            ),
        )

        self.assertLessEqual(package.used_tokens, package.budget_tokens)
        self.assertEqual("core", package.sections[0].name)
        self.assertIn("workspace policy", package.render_markdown())

    def test_reflection_preserves_two_evidence_items(self) -> None:
        self.retain("Release Alpha is July 15.")
        self.retain(" release  alpha  is july 15! ")

        observations = self.container.reflection.reflect(self.tenant, self.workspace)

        self.assertEqual(1, len(observations))
        self.assertEqual(2, len(observations[0].evidence_ids))

    def test_worker_router_dispatches_registered_jobs(self) -> None:
        calls: list[str] = []
        router = RetainedEventRouter({"embed": lambda event: calls.append(event.name)})
        event = IntegrationEvent(
            name="memory.retained.v1",
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            payload={"jobs": ["embed", "unknown"]},
        )

        completed = router.handle(event)

        self.assertEqual(("embed",), completed)
        self.assertEqual(["memory.retained.v1"], calls)

    def test_ingestion_is_stably_chunked_and_idempotent(self) -> None:
        command = IngestDocumentCommand(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            text=("Alpha requirements. " * 80) + "\n\n" + ("Release checklist. " * 80),
            origin_uri="file:///alpha.md",
            chunk_size_chars=300,
            chunk_overlap_chars=30,
        )
        first = self.container.ingestion.ingest_text(command)
        second = self.container.ingestion.ingest_text(command)

        self.assertGreater(first.created_count, 1)
        self.assertEqual(0, second.created_count)
        self.assertEqual(first.memory_ids, second.memory_ids)


if __name__ == "__main__":
    unittest.main()
