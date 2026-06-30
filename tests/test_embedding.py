"""Unit tests for versioned embedding worker and EmbeddingService."""

from __future__ import annotations

import unittest
from uuid import uuid4

from memory_plane.adapters.embeddings import FakeEmbeddingClient
from memory_plane.bootstrap import build_in_memory_container
from memory_plane.contracts.dto import RecallQuery, RetainCommand
from memory_plane.domain.models import MemoryLayer, MemoryScope, Provenance


class EmbeddingServiceTest(unittest.TestCase):
    """Verify embedding generation, vector upsert, and reindexing."""

    def setUp(self) -> None:
        self.container = build_in_memory_container()
        self.tenant = uuid4()
        self.workspace = uuid4()
        self.agent = uuid4()

    def test_fake_embedding_client_dimension_and_determinism(self) -> None:
        """Fake client produces vectors of specified length and same input yields same output."""
        client = FakeEmbeddingClient(dimension=128)
        self.assertEqual(128, client.dimension)

        vec1 = client.embed("test text")
        vec2 = client.embed("test text")
        vec3 = client.embed("different text")

        self.assertEqual(128, len(vec1))
        self.assertEqual(vec1, vec2)
        self.assertNotEqual(vec1, vec3)

    def test_process_memory_retained_creates_qdrant_point(self) -> None:
        """Retaining a memory and processing it yields a vector candidate in search."""
        # 1. Retain memory in ledger
        command = RetainCommand(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            agent_id=self.agent,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text="The capital of France is Paris.",
            provenance=Provenance(source_kind="test"),
        )
        result = self.container.retention.retain(command)
        memory_id = result.item.id

        # 2. Run embedding worker handler logic
        self.container.embedding.process_memory_retained(self.tenant, memory_id)

        # 3. Recall using RetrievalService (which queries Qdrant Candidate Source)
        recall_query = RecallQuery(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            text="capital Paris",
        )
        recall_result = self.container.retrieval.recall(recall_query)

        # Ensure qdrant candidate is present with semantic signals
        qdrant_candidates = [
            c for c in recall_result.candidates if "qdrant_hybrid" in c.source
        ]
        self.assertEqual(1, len(qdrant_candidates))
        self.assertEqual(memory_id, qdrant_candidates[0].item.id)
        self.assertGreaterEqual(qdrant_candidates[0].semantic, 0.0)

    def test_process_memory_retained_missing_raises(self) -> None:
        """Processing a non-existent memory ID raises ValueError for worker retries."""
        with self.assertRaises(ValueError):
            self.container.embedding.process_memory_retained(self.tenant, uuid4())

    def test_reindex_all_updates_qdrant(self) -> None:
        """Reindexing a workspace updates Qdrant with new embeddings for all memories."""
        # 1. Retain two memories
        self.container.retention.retain(
            RetainCommand(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                agent_id=self.agent,
                layer=MemoryLayer.SEMANTIC,
                scope=MemoryScope.WORKSPACE,
                kind="fact",
                text="France capital is Paris.",
                provenance=Provenance(source_kind="test"),
            )
        )
        self.container.retention.retain(
            RetainCommand(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                agent_id=self.agent,
                layer=MemoryLayer.SEMANTIC,
                scope=MemoryScope.WORKSPACE,
                kind="fact",
                text="Germany capital is Berlin.",
                provenance=Provenance(source_kind="test"),
            )
        )

        # 2. Run full reindex
        count = self.container.embedding.reindex_all(self.tenant, self.workspace)
        self.assertEqual(2, count)

        # 3. Search and verify both exist in Qdrant
        recall_result = self.container.retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="capital",
            )
        )
        qdrant_candidates = [
            c for c in recall_result.candidates if "qdrant_hybrid" in c.source
        ]
        self.assertEqual(2, len(qdrant_candidates))


if __name__ == "__main__":
    unittest.main()
