"""Integration tests for Qdrant adapter.

These tests require a running Qdrant instance.  Set ``UAM_TEST_QDRANT_URL``
(e.g. ``http://localhost:6333``) to enable them.
"""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

from memory_plane.contracts.dto import RecallQuery
from memory_plane.domain.models import MemoryItem, MemoryLayer, MemoryScope, Provenance

QDRANT_URL = os.getenv("UAM_TEST_QDRANT_URL")
SKIP_REASON = "UAM_TEST_QDRANT_URL not set"

_T = uuid4()
_W = uuid4()
_PROV = Provenance(source_kind="integration-test")


class _StaticEmbeddingClient:
    model_name = "integration-static"
    dimension = 4

    def embed(self, _text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]

    def embed_query(self, _text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]


class _RejectingHeadLedger:
    def is_recallable_head(self, _tenant_id, _item_id) -> bool:  # type: ignore[no-untyped-def]
        return False

    def filter_recallable_heads(self, _tenant_id, _item_ids) -> frozenset:  # type: ignore[no-untyped-def]
        return frozenset()


def _item(text: str, **kw) -> MemoryItem:  # type: ignore[no-untyped-def]
    defaults = dict(
        tenant_id=_T,
        workspace_id=_W,
        layer=MemoryLayer.SEMANTIC,
        scope=MemoryScope.WORKSPACE,
        kind="fact",
        provenance=_PROV,
    )
    defaults.update(kw)
    return MemoryItem(text=text, **defaults)  # type: ignore[arg-type]


@unittest.skipUnless(QDRANT_URL, SKIP_REASON)
class QdrantIntegrationTest(unittest.TestCase):
    """End-to-end Qdrant adapter tests against a real instance."""

    def setUp(self) -> None:
        from memory_plane.adapters.qdrant import QdrantCandidateSource

        self.collection = f"test_{uuid4().hex[:8]}"
        self.source = QdrantCandidateSource(
            url=QDRANT_URL,  # type: ignore[arg-type]
            collection=self.collection,
            dense_dim=4,
        )
        self.source.connect()

    def tearDown(self) -> None:
        try:
            from qdrant_client import QdrantClient  # type: ignore[import-untyped]

            client = QdrantClient(url=QDRANT_URL)
            client.delete_collection(self.collection)
        except Exception:
            pass

    def test_upsert_and_search(self) -> None:
        item = _item("integration test fact")
        self.source.upsert(item, dense_vector=[0.9, 0.1, 0.0, 0.0])

        # Qdrant search requires a vector query.  Since live search raises
        # NotImplementedError (needs WP-04 embedding), we verify upsert worked
        # by querying the Qdrant client directly.
        from qdrant_client import QdrantClient  # type: ignore[import-untyped]

        client = QdrantClient(url=QDRANT_URL)
        result = client.query_points(
            collection_name=self.collection,
            query=[0.9, 0.1, 0.0, 0.0],
            using="dense",
            limit=5,
        ).points
        self.assertEqual(1, len(result))
        self.assertEqual(str(item.id), result[0].id)

    def test_delete_removes_point(self) -> None:
        item = _item("to delete")
        self.source.upsert(item, dense_vector=[0.5, 0.5, 0.0, 0.0])
        self.source.delete(item.id)

        from qdrant_client import QdrantClient  # type: ignore[import-untyped]

        client = QdrantClient(url=QDRANT_URL)
        result = client.query_points(
            collection_name=self.collection,
            query=[0.5, 0.5, 0.0, 0.0],
            using="dense",
            limit=5,
        ).points
        self.assertEqual(0, len(result))

    def test_reindex_replaces_points(self) -> None:
        old = _item("old data")
        self.source.upsert(old, dense_vector=[0.1, 0.1, 0.1, 0.1])

        new = _item("new data")
        self.source.reindex([(new, [0.9, 0.9, 0.0, 0.0])])

        from qdrant_client import QdrantClient  # type: ignore[import-untyped]

        client = QdrantClient(url=QDRANT_URL)
        result = client.query_points(
            collection_name=self.collection,
            query=[0.9, 0.9, 0.0, 0.0],
            using="dense",
            limit=10,
        ).points
        ids = {r.id for r in result}
        self.assertIn(str(new.id), ids)
        self.assertNotIn(str(old.id), ids)

    def test_collection_has_expected_vectors(self) -> None:
        from qdrant_client import QdrantClient  # type: ignore[import-untyped]

        client = QdrantClient(url=QDRANT_URL)
        info = client.get_collection(self.collection)
        self.assertIn("dense", info.config.params.vectors)

    def test_payload_contains_metadata(self) -> None:
        item = _item("metadata check", labels=("alpha",))
        self.source.upsert(item, dense_vector=[0.1, 0.2, 0.3, 0.4])

        from qdrant_client import QdrantClient  # type: ignore[import-untyped]

        client = QdrantClient(url=QDRANT_URL)
        points = client.retrieve(self.collection, ids=[str(item.id)])
        self.assertEqual(1, len(points))
        payload = points[0].payload
        self.assertEqual(str(item.tenant_id), payload["tenant_id"])
        self.assertEqual(["alpha"], payload["labels"])

    def test_live_search_rejects_vector_when_canonical_head_is_stale(self) -> None:
        from memory_plane.adapters.qdrant import QdrantCandidateSource

        source = QdrantCandidateSource(
            url=QDRANT_URL,  # type: ignore[arg-type]
            collection=self.collection,
            dense_dim=4,
            query_embedding_client=_StaticEmbeddingClient(),
            ledger=_RejectingHeadLedger(),  # type: ignore[arg-type]
        )
        source.connect()
        item = _item("stale indexed value")
        source.upsert(item, dense_vector=[1.0, 0.0, 0.0, 0.0])

        results = source.search(
            RecallQuery(tenant_id=_T, workspace_id=_W, text="stale value")
        )

        self.assertEqual((), results)


if __name__ == "__main__":
    unittest.main()
