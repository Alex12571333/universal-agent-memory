"""Unit tests for the Qdrant dense+sparse CandidateSource adapter.

These tests use a lightweight stub that replaces the real qdrant-client
so that the entire test suite runs without any external infrastructure.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from uuid import UUID, uuid4

from memory_plane.adapters.in_memory import InMemoryMemoryStore
from memory_plane.adapters.qdrant import QdrantCandidateSource
from memory_plane.contracts.dto import RecallQuery
from memory_plane.domain.models import MemoryItem, MemoryLayer, MemoryScope, Provenance

_T = UUID(int=1)
_W = UUID(int=2)
_PROV = Provenance(source_kind="test")


class _StaticEmbeddingClient:
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    @property
    def model_name(self) -> str:
        return "static-test-embedding"

    @property
    def dimension(self) -> int:
        return len(self._vector)

    def embed(self, text: str) -> list[float]:
        return list(self._vector)


def _item(
    text: str = "hello world",
    *,
    layer: MemoryLayer = MemoryLayer.SEMANTIC,
    tenant: UUID = _T,
    workspace: UUID = _W,
    labels: tuple[str, ...] = (),
    thread_id: UUID | None = None,
    scope: MemoryScope = MemoryScope.WORKSPACE,
) -> MemoryItem:
    return MemoryItem(
        tenant_id=tenant,
        workspace_id=workspace,
        layer=layer,
        scope=scope,
        kind="fact",
        text=text,
        provenance=_PROV,
        labels=labels,
        thread_id=thread_id,
    )


class QdrantAdapterTest(unittest.TestCase):
    """Verify QdrantCandidateSource contract compliance."""

    def setUp(self) -> None:
        self.source = QdrantCandidateSource(
            url="http://localhost:6333",
            collection="test_memory",
            dense_dim=4,
        )
        # Use the built-in in-memory fallback instead of a real Qdrant instance.
        self.source._use_in_memory_backend()

    # ---- search contract ------------------------------------------------

    def test_search_returns_candidates_with_project_filter(self) -> None:
        """Only items matching tenant+workspace appear in search results."""
        item_a = _item("semantic information")
        item_b = _item("foreign tenant", tenant=uuid4())
        vec = [0.1, 0.2, 0.3, 0.4]
        self.source.upsert(item_a, dense_vector=vec)
        self.source.upsert(item_b, dense_vector=vec)

        query = RecallQuery(tenant_id=_T, workspace_id=_W, text="semantic")
        results = self.source.search(query)

        self.assertTrue(all(c.item.tenant_id == _T for c in results))
        ids = {c.item.id for c in results}
        self.assertIn(item_a.id, ids)
        self.assertNotIn(item_b.id, ids)

    def test_search_empty_collection_returns_empty(self) -> None:
        query = RecallQuery(tenant_id=_T, workspace_id=_W, text="anything")
        results = self.source.search(query)
        self.assertEqual((), results)

    def test_search_respects_layer_filter(self) -> None:
        """When query specifies layers, only matching items are returned."""
        core = _item("core policy", layer=MemoryLayer.CORE)
        semantic = _item("semantic fact", layer=MemoryLayer.SEMANTIC)
        vec = [0.1, 0.2, 0.3, 0.4]
        self.source.upsert(core, dense_vector=vec)
        self.source.upsert(semantic, dense_vector=vec)

        query = RecallQuery(
            tenant_id=_T,
            workspace_id=_W,
            text="policy",
            layers=(MemoryLayer.CORE,),
        )
        results = self.source.search(query)
        layers = {c.item.layer for c in results}
        self.assertEqual({MemoryLayer.CORE}, layers)

    def test_search_respects_label_filter(self) -> None:
        labeled = _item("labeled fact", labels=("alpha", "release"))
        unlabeled = _item("plain fact")
        vec = [0.1, 0.2, 0.3, 0.4]
        self.source.upsert(labeled, dense_vector=vec)
        self.source.upsert(unlabeled, dense_vector=vec)

        query = RecallQuery(
            tenant_id=_T,
            workspace_id=_W,
            text="fact",
            labels=("alpha",),
        )
        results = self.source.search(query)
        self.assertTrue(all("alpha" in c.item.labels for c in results))

    # ---- upsert / delete / reindex --------------------------------------

    def test_upsert_and_search_roundtrip(self) -> None:
        item = _item("unique knowledge")
        self.source.upsert(item, dense_vector=[0.5, 0.5, 0.5, 0.5])

        query = RecallQuery(tenant_id=_T, workspace_id=_W, text="knowledge")
        results = self.source.search(query)
        self.assertEqual(1, len(results))
        self.assertEqual(item.id, results[0].item.id)

    def test_delete_removes_point(self) -> None:
        item = _item("deletable fact")
        self.source.upsert(item, dense_vector=[0.1, 0.2, 0.3, 0.4])
        self.source.delete(item.id)

        query = RecallQuery(tenant_id=_T, workspace_id=_W, text="deletable")
        results = self.source.search(query)
        self.assertEqual((), results)

    def test_mutations_reject_dimension_and_model_mismatch_before_write(self) -> None:
        first = _item("model one")
        second = _item("model two")
        self.source.upsert(
            first,
            dense_vector=[0.1, 0.2, 0.3, 0.4],
            model_name="embedding-v1",
        )

        with self.assertRaisesRegex(ValueError, "dimension mismatch"):
            self.source.upsert(second, dense_vector=[0.1, 0.2])
        with self.assertRaisesRegex(ValueError, "does not match collection model"):
            self.source.upsert(
                second,
                dense_vector=[0.1, 0.2, 0.3, 0.4],
                model_name="embedding-v2",
            )

        self.assertEqual(
            (first.id,),
            tuple(
                row.item.id
                for row in self.source.search(
                    RecallQuery(tenant_id=_T, workspace_id=_W, text="model")
                )
            ),
        )

    def test_canonical_ledger_blocks_stale_point_during_index_lag(self) -> None:
        ledger = InMemoryMemoryStore()
        source = QdrantCandidateSource(
            url="http://localhost:6333",
            collection="test_memory",
            dense_dim=4,
            ledger=ledger,
        )
        source._use_in_memory_backend()
        old = _item("Alpha releases on July 15")
        replacement = old.supersede("Alpha releases on July 16")
        ledger.append(old)
        ledger.append(replacement)
        source.upsert(old, dense_vector=[0.5, 0.5, 0.5, 0.5])

        results = source.search(
            RecallQuery(tenant_id=_T, workspace_id=_W, text="Alpha release")
        )

        self.assertEqual((), results)

    def test_workspace_sync_replaces_workspace_points(self) -> None:
        old = _item("old knowledge")
        self.source.upsert(old, dense_vector=[0.1, 0.2, 0.3, 0.4])

        new_a = _item("new alpha")
        new_b = _item("new beta")
        self.source.sync_workspace(
            _T,
            _W,
            [
                (new_a, [0.5, 0.5, 0.5, 0.5]),
                (new_b, [0.3, 0.3, 0.3, 0.3]),
            ],
        )

        query = RecallQuery(tenant_id=_T, workspace_id=_W, text="knowledge")
        results = self.source.search(query)
        ids = {c.item.id for c in results}
        self.assertNotIn(old.id, ids)

    def test_workspace_sync_preserves_other_workspaces_and_removes_only_stale_ids(self) -> None:
        other_workspace = uuid4()
        stale = _item("stale local")
        foreign = _item("foreign preserved", workspace=other_workspace)
        replacement = _item("fresh local")
        vector = [0.5, 0.5, 0.5, 0.5]
        self.source.upsert(stale, dense_vector=vector)
        self.source.upsert(foreign, dense_vector=vector)

        self.source.sync_workspace(_T, _W, [(replacement, vector)], model_name="model-v2")

        local_ids = {
            row.item.id
            for row in self.source.search(
                RecallQuery(tenant_id=_T, workspace_id=_W, text="local", top_k=10)
            )
        }
        foreign_ids = {
            row.item.id
            for row in self.source.search(
                RecallQuery(
                    tenant_id=_T,
                    workspace_id=other_workspace,
                    text="foreign",
                    top_k=10,
                )
            )
        }
        self.assertEqual({replacement.id}, local_ids)
        self.assertEqual({foreign.id}, foreign_ids)

    def test_empty_workspace_sync_clears_only_requested_workspace(self) -> None:
        other_workspace = uuid4()
        local = _item("local stale")
        foreign = _item("foreign preserved", workspace=other_workspace)
        vector = [0.5, 0.5, 0.5, 0.5]
        self.source.upsert(local, dense_vector=vector)
        self.source.upsert(foreign, dense_vector=vector)

        self.source.sync_workspace(_T, _W, [])

        self.assertEqual(
            (),
            self.source.search(RecallQuery(tenant_id=_T, workspace_id=_W, text="local")),
        )
        self.assertEqual(
            (foreign.id,),
            tuple(
                row.item.id
                for row in self.source.search(
                    RecallQuery(
                        tenant_id=_T,
                        workspace_id=other_workspace,
                        text="foreign",
                    )
                )
            ),
        )

    def test_workspace_sync_rejects_cross_boundary_items_before_mutation(self) -> None:
        existing = _item("existing local")
        foreign = _item("wrong workspace", workspace=uuid4())
        vector = [0.5, 0.5, 0.5, 0.5]
        self.source.upsert(existing, dense_vector=vector)

        with self.assertRaisesRegex(ValueError, "outside its boundary"):
            self.source.sync_workspace(_T, _W, [(foreign, vector)])

        self.assertEqual(
            (existing.id,),
            tuple(
                row.item.id
                for row in self.source.search(
                    RecallQuery(tenant_id=_T, workspace_id=_W, text="existing")
                )
            ),
        )

    # ---- candidate signals ----------------------------------------------

    def test_candidates_carry_semantic_signal(self) -> None:
        item = _item("tested signal")
        self.source.upsert(item, dense_vector=[1.0, 0.0, 0.0, 0.0])

        query = RecallQuery(tenant_id=_T, workspace_id=_W, text="signal")
        results = self.source.search(query)
        self.assertEqual(1, len(results))
        self.assertGreater(results[0].semantic, 0.0)
        self.assertEqual("qdrant_hybrid", results[0].source)

    def test_in_memory_search_uses_query_embedding_when_available(self) -> None:
        source = QdrantCandidateSource(
            url="http://localhost:6333",
            collection="test_memory",
            dense_dim=4,
            query_embedding_client=_StaticEmbeddingClient([1.0, 0.0, 0.0, 0.0]),
        )
        source._use_in_memory_backend()
        relevant = _item("vector relevant")
        unrelated = _item("vector unrelated")
        source.upsert(unrelated, dense_vector=[0.0, 1.0, 0.0, 0.0])
        source.upsert(relevant, dense_vector=[1.0, 0.0, 0.0, 0.0])

        results = source.search(RecallQuery(tenant_id=_T, workspace_id=_W, text="anything"))

        self.assertEqual(relevant.id, results[0].item.id)
        self.assertGreater(results[0].semantic, results[1].semantic)

    def test_name_property_is_stable(self) -> None:
        self.assertEqual("qdrant_hybrid", self.source.name)

    # ---- fusion with lexical source via RetrievalService -----------------

    def test_fusion_with_lexical_source(self) -> None:
        """QdrantCandidateSource cooperates with RetrievalService fusion."""
        from memory_plane.adapters.in_memory import InMemoryMemoryStore
        from memory_plane.services.retrieval import RetrievalService

        store = InMemoryMemoryStore()
        item = _item("python primary language")
        store.append(item)
        self.source.upsert(item, dense_vector=[0.9, 0.1, 0.0, 0.0])

        retrieval = RetrievalService((store, self.source))
        result = retrieval.recall(
            RecallQuery(tenant_id=_T, workspace_id=_W, text="python language")
        )

        self.assertGreater(len(result.candidates), 0)
        self.assertIn("qdrant_hybrid", result.sources_used)
        self.assertIn("sql_lexical", result.sources_used)

    def test_upsert_qdrant_adds_model_name_to_payload(self) -> None:
        """Verify model_name is passed to Qdrant payload when provided."""
        from unittest.mock import MagicMock, patch

        mock_models = MagicMock()
        mock_models.PointStruct = lambda id, vector, payload: MagicMock(payload=payload)

        with patch.dict(
            "sys.modules",
            {
                "qdrant_client": MagicMock(),
                "qdrant_client.models": mock_models,
            },
        ):
            source = QdrantCandidateSource(
                url="http://localhost:6333", collection="test", dense_dim=4
            )
            source._client = MagicMock()
            item = _item("test text")

            source._upsert_qdrant(item, [0.1, 0.2, 0.3, 0.4], model_name="test-model-v2")

            called_args = source._client.upsert.call_args
            self.assertIsNotNone(called_args)
            points = called_args.kwargs.get("points")
            self.assertEqual(1, len(points))
            self.assertEqual("test-model-v2", points[0].payload.get("model_name"))

    def test_upsert_qdrant_can_redact_text_payload(self) -> None:
        """Production mode can keep raw text out of Qdrant payloads."""
        from unittest.mock import MagicMock, patch

        mock_models = MagicMock()
        mock_models.PointStruct = lambda id, vector, payload: MagicMock(payload=payload)

        with patch.dict(
            "sys.modules",
            {
                "qdrant_client": MagicMock(),
                "qdrant_client.models": mock_models,
            },
        ):
            source = QdrantCandidateSource(
                url="http://localhost:6333",
                collection="test",
                dense_dim=4,
                payload_text=False,
            )
            source._client = MagicMock()
            item = _item("sensitive agent memory")

            source._upsert_qdrant(item, [0.1, 0.2, 0.3, 0.4])

            points = source._client.upsert.call_args.kwargs["points"]
            payload = points[0].payload
            self.assertNotIn("text", payload)
            self.assertTrue(payload["text_redacted"])
            self.assertEqual(str(item.id), payload["memory_id"])

    def test_live_qdrant_search_embeds_query_and_maps_payload(self) -> None:
        """Live Qdrant recall uses a real query vector instead of raising."""
        from unittest.mock import MagicMock, patch

        mock_models = MagicMock()
        mock_models.FieldCondition = lambda key, match: ("field", key, match)
        mock_models.Filter = lambda must: ("filter", must)
        mock_models.MatchValue = lambda value: ("match", value)

        with patch.dict(
            "sys.modules",
            {
                "qdrant_client": MagicMock(),
                "qdrant_client.models": mock_models,
            },
        ):
            source = QdrantCandidateSource(
                url="http://localhost:6333",
                collection="test",
                dense_dim=4,
                query_embedding_client=_StaticEmbeddingClient([0.9, 0.1, 0.0, 0.0]),
            )
            item = _item("production q8 embeddings")
            source._client = MagicMock()
            source._client.query_points = None
            source._client.search.return_value = [
                SimpleNamespace(
                    payload=QdrantCandidateSource._item_to_payload(item),
                    score=0.91,
                )
            ]

            results = source.search(
                RecallQuery(tenant_id=_T, workspace_id=_W, text="q8 embeddings")
            )

            self.assertEqual(1, len(results))
            self.assertEqual(item.id, results[0].item.id)
            self.assertEqual(0.91, results[0].semantic)
            search_kwargs = source._client.search.call_args.kwargs
            self.assertEqual(("dense", [0.9, 0.1, 0.0, 0.0]), search_kwargs["query_vector"])
            self.assertEqual("test", search_kwargs["collection_name"])

    def test_live_qdrant_search_hydrates_redacted_payload_from_ledger(self) -> None:
        """Recall still returns text when Qdrant payloads omit raw memory text."""
        from unittest.mock import MagicMock, patch

        from memory_plane.adapters.in_memory import InMemoryMemoryStore

        mock_models = MagicMock()
        mock_models.FieldCondition = lambda key, match: ("field", key, match)
        mock_models.Filter = lambda must: ("filter", must)
        mock_models.MatchValue = lambda value: ("match", value)

        with patch.dict(
            "sys.modules",
            {
                "qdrant_client": MagicMock(),
                "qdrant_client.models": mock_models,
            },
        ):
            store = InMemoryMemoryStore()
            item = _item("production q8 embeddings stay editable")
            store.append(item)
            source = QdrantCandidateSource(
                url="http://localhost:6333",
                collection="test",
                dense_dim=4,
                query_embedding_client=_StaticEmbeddingClient([0.9, 0.1, 0.0, 0.0]),
                ledger=store,
                payload_text=False,
            )
            source._client = MagicMock()
            source._client.query_points = None
            source._client.search.return_value = [
                SimpleNamespace(
                    payload=QdrantCandidateSource._item_to_payload(
                        item,
                        include_text=False,
                    ),
                    score=0.91,
                )
            ]

            results = source.search(
                RecallQuery(tenant_id=_T, workspace_id=_W, text="q8 embeddings")
            )

            self.assertEqual(1, len(results))
            self.assertEqual(item.id, results[0].item.id)
            self.assertEqual("production q8 embeddings stay editable", results[0].item.text)
            self.assertGreater(results[0].lexical, 0)

    def test_live_qdrant_search_supports_query_points_client_api(self) -> None:
        """qdrant-client 1.18+ uses query_points instead of search."""
        from unittest.mock import MagicMock, patch

        mock_models = MagicMock()
        mock_models.FieldCondition = lambda key, match: ("field", key, match)
        mock_models.Filter = lambda must: ("filter", must)
        mock_models.MatchValue = lambda value: ("match", value)

        class QueryPointsOnlyClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def query_points(self, **kwargs: object) -> object:
                self.calls.append(kwargs)
                return SimpleNamespace(
                    points=[
                        SimpleNamespace(
                            payload=QdrantCandidateSource._item_to_payload(
                                _item("qdrant query points")
                            ),
                            score=0.88,
                        )
                    ]
                )

        with patch.dict(
            "sys.modules",
            {
                "qdrant_client": MagicMock(),
                "qdrant_client.models": mock_models,
            },
        ):
            source = QdrantCandidateSource(
                url="http://localhost:6333",
                collection="test",
                dense_dim=4,
                query_embedding_client=_StaticEmbeddingClient([0.9, 0.1, 0.0, 0.0]),
            )
            client = QueryPointsOnlyClient()
            source._client = client

            results = source.search(
                RecallQuery(tenant_id=_T, workspace_id=_W, text="qdrant")
            )

            self.assertEqual(1, len(results))
            self.assertEqual(0.88, results[0].semantic)
            self.assertEqual([0.9, 0.1, 0.0, 0.0], client.calls[0]["query"])
            self.assertEqual("dense", client.calls[0]["using"])

    def test_live_qdrant_search_queries_every_requested_layer(self) -> None:
        """A multi-layer recall must not silently become a first-layer recall."""
        from unittest.mock import MagicMock, patch

        mock_models = MagicMock()
        mock_models.FieldCondition = lambda key, match: ("field", key, match)
        mock_models.Filter = lambda must: ("filter", must)
        mock_models.MatchValue = lambda value: ("match", value)

        class QueryPointsClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def query_points(self, **kwargs: object) -> object:
                self.calls.append(kwargs)
                return SimpleNamespace(points=[])

        with patch.dict(
            "sys.modules",
            {"qdrant_client": MagicMock(), "qdrant_client.models": mock_models},
        ):
            source = QdrantCandidateSource(
                url="http://localhost:6333",
                collection="test",
                dense_dim=4,
                query_embedding_client=_StaticEmbeddingClient([0.9, 0.1, 0.0, 0.0]),
            )
            client = QueryPointsClient()
            source._client = client

            source.search(
                RecallQuery(
                    tenant_id=_T,
                    workspace_id=_W,
                    text="qdrant",
                    layers=(MemoryLayer.CORE, MemoryLayer.SEMANTIC),
                )
            )

            self.assertEqual(2, len(client.calls))


if __name__ == "__main__":
    unittest.main()
