"""Unit tests for versioned embedding worker and EmbeddingService."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import ANY, patch
from uuid import uuid4

from memory_plane.adapters.embeddings import (
    EmbeddingProviderConfig,
    FakeEmbeddingClient,
    build_embedding_client,
)
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

    def test_embedding_provider_factory_selects_fake(self) -> None:
        """Provider factory keeps deterministic fake as the default local mode."""
        client = build_embedding_client(
            EmbeddingProviderConfig(
                provider="fake",
                model_name="test-fake",
                dimension=8,
            )
        )

        self.assertIsInstance(client, FakeEmbeddingClient)
        self.assertEqual("test-fake", client.model_name)
        self.assertEqual(8, client.dimension)

    def test_openai_embedding_client_posts_expected_payload(self) -> None:
        """OpenAI provider calls `/embeddings` and extracts `data[0].embedding`."""
        captured: dict[str, Any] = {}

        def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["authorization"] = request.headers["Authorization"]
            captured["timeout"] = timeout
            return _FakeResponse({"data": [{"embedding": [0.1, 0.2]}]})

        with patch("memory_plane.adapters.embeddings.urlopen", fake_urlopen):
            client = build_embedding_client(
                EmbeddingProviderConfig(
                    provider="openai",
                    model_name="text-embedding-3-small",
                    dimension=2,
                    base_url="https://api.example/v1",
                    api_key="secret",
                    timeout_seconds=7,
                )
            )
            vector = client.embed("hello")

        self.assertEqual([0.1, 0.2], vector)
        self.assertEqual("https://api.example/v1/embeddings", captured["url"])
        self.assertEqual(
            {"model": "text-embedding-3-small", "input": "hello", "dimensions": 2},
            captured["payload"],
        )
        self.assertEqual("Bearer secret", captured["authorization"])
        self.assertEqual(7, captured["timeout"])

    def test_ollama_embedding_client_posts_prompt_payload(self) -> None:
        """Ollama provider uses the local `/api/embeddings` shape."""
        captured: dict[str, Any] = {}

        def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({"embedding": [0, 1, 2]})

        with patch("memory_plane.adapters.embeddings.urlopen", fake_urlopen):
            client = build_embedding_client(
                EmbeddingProviderConfig(
                    provider="ollama",
                    model_name="nomic-embed-text",
                    dimension=3,
                    base_url="http://ollama:11434",
                )
            )
            vector = client.embed("local text")

        self.assertEqual([0.0, 1.0, 2.0], vector)
        self.assertEqual("http://ollama:11434/api/embeddings", captured["url"])
        self.assertEqual(
            {"model": "nomic-embed-text", "prompt": "local text"},
            captured["payload"],
        )

    def test_tei_embedding_client_posts_openai_compatible_payload(self) -> None:
        """TEI provider uses OpenAI-compatible `/v1/embeddings` without requiring a key."""
        captured: dict[str, Any] = {}

        def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["headers"] = dict(request.headers)
            return _FakeResponse({"data": [{"embedding": [3, 4]}]})

        with patch("memory_plane.adapters.embeddings.urlopen", fake_urlopen):
            client = build_embedding_client(
                EmbeddingProviderConfig(
                    provider="tei",
                    model_name="bge",
                    dimension=2,
                    base_url="http://tei:8080",
                )
            )
            vector = client.embed("document")

        self.assertEqual([3.0, 4.0], vector)
        self.assertEqual("http://tei:8080/v1/embeddings", captured["url"])
        self.assertEqual({"model": "bge", "input": "document"}, captured["payload"])
        self.assertNotIn("Authorization", captured["headers"])

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

        # 2. Run embedding worker handler logic with a spy
        qdrant = self.container.embedding._qdrant
        with patch.object(qdrant, "upsert", wraps=qdrant.upsert) as mock_upsert:
            self.container.embedding.process_memory_retained(self.tenant, memory_id)
            mock_upsert.assert_called_once_with(
                ANY,
                dense_vector=ANY,
                model_name="fake-embed-v1",
            )

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

    def test_process_memory_retained_rejects_dimension_mismatch(self) -> None:
        """Provider output dimension is validated before indexing."""
        result = self.container.retention.retain(
            RetainCommand(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                layer=MemoryLayer.SEMANTIC,
                scope=MemoryScope.WORKSPACE,
                kind="fact",
                text="Mismatch should not be indexed.",
                provenance=Provenance(source_kind="test"),
            )
        )
        self.container.embedding._client = _WrongDimensionClient()
        qdrant = self.container.embedding._qdrant
        with patch.object(qdrant, "upsert", wraps=qdrant.upsert) as mock_upsert:
            with self.assertRaisesRegex(ValueError, "dimension mismatch"):
                self.container.embedding.process_memory_retained(
                    self.tenant,
                    result.item.id,
                )
            mock_upsert.assert_not_called()

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

        # 2. Run full reindex with a spy
        qdrant = self.container.embedding._qdrant
        with patch.object(qdrant, "reindex", wraps=qdrant.reindex) as mock_reindex:
            count = self.container.embedding.reindex_all(self.tenant, self.workspace)
            mock_reindex.assert_called_once_with(
                ANY,
                model_name="fake-embed-v1",
            )
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


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _WrongDimensionClient:
    @property
    def model_name(self) -> str:
        return "wrong-dim"

    @property
    def dimension(self) -> int:
        return 3

    def embed(self, text: str) -> list[float]:
        return [1.0, 2.0]
