"""Qdrant dense+sparse CandidateSource implementation.

Uses the ``qdrant-client`` library to perform hybrid search against a Qdrant
instance running in the ``advanced`` Docker Compose profile.  Falls back to an
in-memory stub for unit tests and local development without infrastructure.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from hashlib import blake2b
from math import sqrt
from threading import RLock
from typing import TYPE_CHECKING, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from memory_plane.contracts.dto import Candidate, RecallQuery
from memory_plane.domain.models import (
    MemoryItem,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    Provenance,
)
from memory_plane.ports.embeddings import EmbeddingClient

if TYPE_CHECKING:
    from memory_plane.ports.repositories import MemoryLedger

_WORD = re.compile(r"\w+", re.UNICODE)


class _ScoredPoint(Protocol):
    """Subset of a Qdrant result consumed by the retrieval adapter."""

    payload: dict[str, object] | None
    score: float


class _SearchMethod(Protocol):
    """Legacy qdrant-client search call supported by the adapter."""

    def __call__(
        self,
        *,
        collection_name: str,
        query_vector: tuple[str, list[float]],
        query_filter: object,
        limit: int,
        with_payload: bool,
    ) -> Sequence[_ScoredPoint]: ...


class _QueryPointsResponse(Protocol):
    """Subset of a qdrant-client query response consumed by the adapter."""

    points: Sequence[_ScoredPoint]


class _QueryPointsClient(Protocol):
    """Modern qdrant-client query call supported by the adapter."""

    def query_points(self, **kwargs: object) -> _QueryPointsResponse: ...


class _QueryEmbeddingMethod(Protocol):
    """Optional query-specific embedding method exposed by some providers."""

    def __call__(self, text: str) -> list[float]: ...


_NumericPayload = int | float | str


class QdrantCandidateSource:
    """Production retrieval adapter that performs hybrid dense+sparse search.

    The adapter is designed to operate behind the ``CandidateSource`` protocol
    so it integrates transparently with ``RetrievalService`` fusion.

    Dense vectors are stored under the ``"dense"`` named vector, and sparse
    vectors under the ``"sparse"`` named vector. Payload carries project-scoped
    filter metadata. Production deployments can disable raw text payloads and
    hydrate recalled candidates from the canonical ledger.
    """

    def __init__(
        self,
        url: str,
        collection: str = "memory_items",
        *,
        dense_dim: int = 1536,
        api_key: str | None = None,
        query_embedding_client: EmbeddingClient | None = None,
        ledger: MemoryLedger | None = None,
        payload_text: bool = True,
    ) -> None:
        """Capture endpoint and collection; delay client creation until ``connect``."""
        self.url = url
        self.collection = collection
        self.dense_dim = dense_dim
        self.api_key = api_key
        self._query_embedding_client = query_embedding_client
        self._ledger = ledger
        self._payload_text = payload_text
        self._client: object | None = None  # QdrantClient when connected
        self._collection_model_name: str | None = None
        # In-memory fallback for tests (activated by ``_use_in_memory_backend``).
        self._mem_items: dict[UUID, tuple[MemoryItem, list[float]]] | None = None
        self._mem_lock: RLock | None = None

    @property
    def name(self) -> str:
        """Return the source identifier expected in recall diagnostics."""
        return "qdrant_hybrid"

    # ---- lifecycle ------------------------------------------------------

    def connect(self) -> None:
        """Create the Qdrant client and ensure the collection exists.

        Creates a collection with ``dense`` (cosine, ``dense_dim`` dimensions)
        and ``sparse`` named vectors if it does not already exist.
        """
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import (
                Distance,
                SparseVectorParams,
                VectorParams,
            )
        except ImportError as exc:
            raise RuntimeError(
                "qdrant-client is required for the Qdrant adapter. "
                "Install it with: pip install 'universal-agent-memory[qdrant]'"
            ) from exc

        self._client = QdrantClient(url=self.url, api_key=self.api_key)
        collections = {c.name for c in self._client.get_collections().collections}  # type: ignore[union-attr]
        if self.collection not in collections:
            self._client.create_collection(  # type: ignore[union-attr]
                collection_name=self.collection,
                vectors_config={
                    "dense": VectorParams(size=self.dense_dim, distance=Distance.COSINE),
                },
                sparse_vectors_config={
                    "sparse": SparseVectorParams(),
                },
            )
        self._validate_collection_identity()

    def _use_in_memory_backend(self) -> None:
        """Activate a minimal in-memory backend for unit tests.

        This avoids any dependency on ``qdrant-client`` while still exercising
        the search/filter logic faithfully.
        """
        self._mem_items = {}
        self._mem_lock = RLock()

    # ---- CandidateSource protocol ---------------------------------------

    def search(self, query: RecallQuery) -> tuple[Candidate, ...]:
        """Produce candidates with project filter and hybrid scoring.

        When running against a real Qdrant instance, this issues a hybrid
        query with separate ``dense`` and ``sparse`` prefetch stages and a
        fusion step.  The in-memory fallback replicates the same filtering
        logic with cosine similarity computed locally.
        """
        if self._mem_items is not None:
            return self._search_in_memory(query)
        return self._search_qdrant(query)

    # ---- mutation methods (not part of CandidateSource protocol) --------

    def upsert(
        self,
        item: MemoryItem,
        dense_vector: list[float],
        sparse_indices: list[int] | None = None,
        sparse_values: list[float] | None = None,
        model_name: str | None = None,
    ) -> None:
        """Insert or update a point with full metadata payload."""
        self._validate_vector_dimensions(((item, dense_vector),))
        self._validate_write_model(model_name)
        if self._mem_items is not None:
            return self._upsert_in_memory(item, dense_vector, model_name)
        self._upsert_qdrant(item, dense_vector, sparse_indices, sparse_values, model_name)

    def delete(self, item_id: UUID) -> None:
        """Remove a point by its memory item ID."""
        if self._mem_items is not None:
            return self._delete_in_memory(item_id)
        self._delete_qdrant(item_id)

    def sync_workspace(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        items: Sequence[tuple[MemoryItem, list[float]]],
        model_name: str | None = None,
    ) -> None:
        """Replace one workspace's vector set without touching other workspaces."""
        for item, _vector in items:
            if item.tenant_id != tenant_id or item.workspace_id != workspace_id:
                raise ValueError("workspace sync contains an item outside its boundary")
        self._validate_vector_dimensions(items)
        self._validate_write_model(model_name)
        if self._mem_items is not None:
            self._sync_workspace_in_memory(tenant_id, workspace_id, items)
            return
        self._sync_workspace_qdrant(tenant_id, workspace_id, items, model_name)

    def count_workspace_points(self, tenant_id: UUID, workspace_id: UUID) -> int:
        """Count indexed memory points inside one boundary, excluding metadata."""
        if self._mem_items is not None:
            return sum(
                item.tenant_id == tenant_id and item.workspace_id == workspace_id
                for item, _vector in self._mem_items.values()
            )
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        result = self._client.count(  # type: ignore[union-attr]
            collection_name=self.collection,
            count_filter=Filter(
                must=[
                    FieldCondition(key="tenant_id", match=MatchValue(value=str(tenant_id))),
                    FieldCondition(
                        key="workspace_id",
                        match=MatchValue(value=str(workspace_id)),
                    ),
                ]
            ),
            exact=True,
        )
        return int(result.count)

    def _validate_collection_identity(self) -> None:
        """Reject a collection whose vector or embedding-model identity is incompatible."""
        from qdrant_client.models import PointStruct

        info = self._client.get_collection(self.collection)  # type: ignore[union-attr]
        vectors = info.config.params.vectors
        dense = vectors.get("dense") if isinstance(vectors, dict) else None
        actual_dimension = getattr(dense, "size", None)
        if actual_dimension != self.dense_dim:
            raise RuntimeError(
                f"Qdrant collection {self.collection!r} dense dimension is "
                f"{actual_dimension!r}; expected {self.dense_dim}. "
                "Select a new collection and run a controlled migration."
            )

        expected_model = getattr(self._query_embedding_client, "model_name", None)
        if not expected_model:
            return
        metadata_id = self._metadata_point_id()
        metadata = self._client.retrieve(  # type: ignore[union-attr]
            self.collection,
            ids=[str(metadata_id)],
            with_payload=True,
            with_vectors=False,
        )
        if metadata:
            payload = metadata[0].payload or {}
            actual_model = payload.get("model_name")
            actual_meta_dimension = payload.get("dimension")
            if actual_model != expected_model or actual_meta_dimension != self.dense_dim:
                raise RuntimeError(
                    f"Qdrant collection {self.collection!r} belongs to model "
                    f"{actual_model!r}/{actual_meta_dimension!r}, expected "
                    f"{expected_model!r}/{self.dense_dim}. Select a new collection "
                    "and run a controlled migration."
                )
            self._collection_model_name = str(actual_model)
            return

        observed_models = self._scan_existing_model_names()
        if observed_models and observed_models != {expected_model}:
            raise RuntimeError(
                f"Qdrant collection {self.collection!r} contains model identities "
                f"{sorted(observed_models)!r}, expected {expected_model!r}. "
                "Select a new collection and run a controlled migration."
            )
        self._client.upsert(  # type: ignore[union-attr]
            collection_name=self.collection,
            points=[
                PointStruct(
                    id=str(metadata_id),
                    vector={"dense": [0.0] * self.dense_dim},
                    payload={
                        "_uam_record_type": "collection_metadata",
                        "model_name": expected_model,
                        "dimension": self.dense_dim,
                    },
                )
            ],
        )
        self._collection_model_name = expected_model

    def _scan_existing_model_names(self) -> set[str]:
        """Read legacy point metadata before adopting a collection identity."""
        models: set[str] = set()
        missing_model = False
        offset: object | None = None
        while True:
            points, next_offset = self._client.scroll(  # type: ignore[union-attr]
                collection_name=self.collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                if payload.get("_uam_record_type") == "collection_metadata":
                    continue
                model_name = payload.get("model_name")
                if model_name:
                    models.add(str(model_name))
                else:
                    missing_model = True
            if next_offset is None:
                break
            offset = next_offset
        if missing_model:
            raise RuntimeError(
                f"Qdrant collection {self.collection!r} contains points without a "
                "verifiable embedding model. Select a new collection and reindex."
            )
        return models

    def _validate_write_model(self, model_name: str | None) -> None:
        """Prevent mixed embedding models inside one collection."""
        if model_name is None:
            if self._collection_model_name is not None:
                raise ValueError("model_name is required for an identified Qdrant collection")
            return
        if self._collection_model_name is None:
            self._collection_model_name = model_name
            return
        if model_name != self._collection_model_name:
            raise ValueError(
                f"embedding model {model_name!r} does not match collection model "
                f"{self._collection_model_name!r}"
            )

    def _validate_vector_dimensions(
        self,
        items: Sequence[tuple[MemoryItem, list[float]]],
    ) -> None:
        """Reject malformed vectors before any index mutation."""
        for _item, vector in items:
            if len(vector) != self.dense_dim:
                raise ValueError(
                    f"dense vector dimension mismatch: expected {self.dense_dim}, "
                    f"got {len(vector)}"
                )

    def _metadata_point_id(self) -> UUID:
        return uuid5(NAMESPACE_URL, f"obelisk-memory:qdrant:{self.collection}:metadata")

    # ---- in-memory fallback implementation ------------------------------

    def _upsert_in_memory(
        self,
        item: MemoryItem,
        dense_vector: list[float],
        model_name: str | None = None,
    ) -> None:
        assert self._mem_items is not None and self._mem_lock is not None
        with self._mem_lock:
            self._mem_items[item.id] = (item, dense_vector)

    def _delete_in_memory(self, item_id: UUID) -> None:
        assert self._mem_items is not None and self._mem_lock is not None
        with self._mem_lock:
            self._mem_items.pop(item_id, None)

    def _sync_workspace_in_memory(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        items: Sequence[tuple[MemoryItem, list[float]]],
    ) -> None:
        assert self._mem_items is not None and self._mem_lock is not None
        replacement = {item.id: (item, vector) for item, vector in items}
        with self._mem_lock:
            stale_ids = {
                item_id
                for item_id, (item, _vector) in self._mem_items.items()
                if item.tenant_id == tenant_id and item.workspace_id == workspace_id
                and item_id not in replacement
            }
            self._mem_items.update(replacement)
            for item_id in stale_ids:
                self._mem_items.pop(item_id, None)

    def _search_in_memory(self, query: RecallQuery) -> tuple[Candidate, ...]:
        """In-memory search with cosine similarity and metadata filtering."""
        assert self._mem_items is not None and self._mem_lock is not None
        query_terms = self._terms(query.text)
        query_vector = (
            self._embed_query(query.text) if self._query_embedding_client is not None else None
        )
        results: list[Candidate] = []
        with self._mem_lock:
            stored = tuple(self._mem_items.values())
            superseded_ids = {
                item.supersedes_id
                for item, _ in stored
                if item.supersedes_id is not None
            }
            recallable_ids = self._recallable_head_ids(tuple(item for item, _ in stored))
            for item, vec in stored:
                if item.id in superseded_ids or item.id not in recallable_ids:
                    continue
                if not self._matches_filter(item, query):
                    continue
                # Cosine similarity against the real query vector when an
                # embedding client is wired; otherwise use the deterministic
                # fallback required by unit tests.
                if query_vector is not None:
                    semantic = self._bounded_cosine(query_vector, vec)
                else:
                    semantic = max(0.0, min(1.0, sum(v for v in vec) / max(1, len(vec))))
                # Lexical overlap as sparse-vector proxy.
                item_terms = self._terms(item.text)
                overlap = len(query_terms & item_terms)
                lexical = overlap / max(1, len(query_terms))
                results.append(
                    Candidate(
                        item=item,
                        source=self.name,
                        semantic=semantic,
                        lexical=lexical,
                        entity=lexical,
                        trust=item.confidence,
                    )
                )
        results.sort(key=lambda c: c.semantic + c.lexical, reverse=True)
        return tuple(results[: query.top_k])

    # ---- real Qdrant implementation -------------------------------------

    def _upsert_qdrant(
        self,
        item: MemoryItem,
        dense_vector: list[float],
        sparse_indices: list[int] | None = None,
        sparse_values: list[float] | None = None,
        model_name: str | None = None,
    ) -> None:
        from qdrant_client.models import PointStruct, SparseVector

        vectors: dict[str, object] = {"dense": dense_vector}
        if sparse_indices is None or sparse_values is None:
            sparse_indices, sparse_values = self._sparse_vector(item.text)
        vectors["sparse"] = SparseVector(indices=sparse_indices, values=sparse_values)

        payload = self._item_to_payload(item, include_text=self._payload_text)
        if model_name:
            payload["model_name"] = model_name

        point = PointStruct(
            id=str(item.id),
            vector=vectors,
            payload=payload,
        )
        self._client.upsert(collection_name=self.collection, points=[point])  # type: ignore[union-attr]

    def _delete_qdrant(self, item_id: UUID) -> None:
        from qdrant_client.models import PointIdsList

        self._client.delete(  # type: ignore[union-attr]
            collection_name=self.collection,
            points_selector=PointIdsList(points=[str(item_id)]),
        )

    def _sync_workspace_qdrant(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        items: Sequence[tuple[MemoryItem, list[float]]],
        model_name: str | None = None,
    ) -> None:
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchValue,
            PointIdsList,
            PointStruct,
            SparseVector,
        )

        workspace_filter = Filter(
            must=[
                FieldCondition(key="tenant_id", match=MatchValue(value=str(tenant_id))),
                FieldCondition(key="workspace_id", match=MatchValue(value=str(workspace_id))),
            ]
        )
        existing_ids: set[UUID] = set()
        offset: object | None = None
        while True:
            points, next_offset = self._client.scroll(  # type: ignore[union-attr]
                collection_name=self.collection,
                scroll_filter=workspace_filter,
                limit=256,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            existing_ids.update(UUID(str(point.id)) for point in points)
            if next_offset is None:
                break
            offset = next_offset

        batch: list[PointStruct] = []
        for item, vector in items:
            payload = self._item_to_payload(item, include_text=self._payload_text)
            if model_name:
                payload["model_name"] = model_name
            sparse_indices, sparse_values = self._sparse_vector(item.text)
            batch.append(
                PointStruct(
                    id=str(item.id),
                    vector={
                        "dense": vector,
                        "sparse": SparseVector(
                            indices=sparse_indices,
                            values=sparse_values,
                        ),
                    },
                    payload=payload,
                )
            )
            if len(batch) >= 100:
                self._client.upsert(collection_name=self.collection, points=batch)  # type: ignore[union-attr]
                batch = []
        if batch:
            self._client.upsert(collection_name=self.collection, points=batch)  # type: ignore[union-attr]

        replacement_ids = {item.id for item, _vector in items}
        stale_ids = existing_ids - replacement_ids
        if stale_ids:
            self._client.delete(  # type: ignore[union-attr]
                collection_name=self.collection,
                points_selector=PointIdsList(points=[str(item_id) for item_id in stale_ids]),
            )

    def _search_qdrant(self, query: RecallQuery) -> tuple[Candidate, ...]:
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchValue,
        )

        if self._query_embedding_client is None:
            raise NotImplementedError(
                "Live Qdrant search requires a query embedding client. "
                "Pass query_embedding_client when constructing QdrantCandidateSource."
            )

        query_vector = self._embed_query(query.text)
        if len(query_vector) != self.dense_dim:
            raise RuntimeError(
                f"query embedding dimension mismatch: expected {self.dense_dim}, "
                f"got {len(query_vector)}"
            )

        must_conditions: list[FieldCondition] = [
            FieldCondition(key="tenant_id", match=MatchValue(value=str(query.tenant_id))),
            FieldCondition(key="workspace_id", match=MatchValue(value=str(query.workspace_id))),
        ]
        if query.labels:
            for label in query.labels:
                must_conditions.append(FieldCondition(key="labels", match=MatchValue(value=label)))

        # Query once per requested layer rather than silently retaining only
        # the first layer. This works across supported qdrant-client versions
        # without relying on version-specific MatchAny/MinShould models.
        filters = (
            tuple(
                Filter(
                    must=[
                        *must_conditions,
                        FieldCondition(key="layer", match=MatchValue(value=str(layer))),
                    ]
                )
                for layer in query.layers
            )
            if query.layers
            else (Filter(must=must_conditions),)
        )
        sparse_indices, sparse_values = self._sparse_vector(query.text)
        rows = tuple(
            row
            for query_filter in filters
            for row in self._query_hybrid_points(
                query_vector=query_vector,
                sparse_indices=sparse_indices,
                sparse_values=sparse_values,
                query_filter=query_filter,
                limit=max(query.top_k * 3, query.top_k),
            )
        )
        candidates: list[Candidate] = []
        query_terms = self._terms(query.text)
        hydrated = tuple(
            (row, self._payload_to_candidate_item(row.payload or {})) for row in rows
        )
        recallable_ids = self._recallable_head_ids(tuple(item for _, item in hydrated))
        for row, item in hydrated:
            if item.id not in recallable_ids:
                continue
            if not self._matches_filter(item, query):
                continue
            item_terms = self._terms(item.text)
            overlap = len(query_terms & item_terms)
            lexical = overlap / max(1, len(query_terms))
            candidates.append(
                Candidate(
                    item=item,
                    source=self.name,
                    semantic=max(0.0, min(1.0, float(row.score))),
                    lexical=lexical,
                    entity=lexical,
                    trust=item.confidence,
                )
            )
            if len(candidates) >= query.top_k:
                break
        return tuple(candidates)

    # ---- helpers --------------------------------------------------------

    def _recallable_head_ids(self, items: tuple[MemoryItem, ...]) -> frozenset[UUID]:
        """Consult PostgreSQL once so eventually stale vector points cannot leak."""
        if self._ledger is None:
            return frozenset(item.id for item in items)
        if not items:
            return frozenset()
        checker = getattr(self._ledger, "filter_recallable_heads", None)
        if callable(checker):
            by_tenant: dict[UUID, list[UUID]] = {}
            for item in items:
                by_tenant.setdefault(item.tenant_id, []).append(item.id)
            allowed: set[UUID] = set()
            for tenant_id, item_ids in by_tenant.items():
                allowed.update(checker(tenant_id, tuple(item_ids)))
            return frozenset(allowed)
        single_checker = getattr(self._ledger, "is_recallable_head", None)
        if callable(single_checker):
            return frozenset(
                item.id
                for item in items
                if single_checker(item.tenant_id, item.id)
            )
        return frozenset(item.id for item in items)

    @staticmethod
    def _matches_filter(item: MemoryItem, query: RecallQuery) -> bool:
        """Apply project-scoped and metadata filters."""
        if item.tenant_id != query.tenant_id:
            return False
        if item.workspace_id != query.workspace_id:
            return False
        if query.layers and item.layer not in query.layers:
            return False
        if query.labels and not set(query.labels).issubset(item.labels):
            return False
        if item.scope == MemoryScope.THREAD and item.thread_id != query.thread_id:
            return False
        if item.scope == MemoryScope.PRIVATE and item.agent_id != query.agent_id:
            return False
        if item.status in (MemoryStatus.REJECTED, MemoryStatus.ARCHIVED):
            return False
        return True

    @staticmethod
    def _terms(text: str) -> set[str]:
        """Tokenize text for lexical overlap scoring."""
        return {m.group(0).casefold() for m in _WORD.finditer(text)}

    @staticmethod
    def _sparse_vector(text: str) -> tuple[list[int], list[float]]:
        """Create a stable hashed sparse representation without retaining text.

        Qdrant stores only index/value pairs. The deterministic 32-bit hashing
        is intentionally a retrieval aid, not a security boundary; canonical
        text and access decisions remain in PostgreSQL.
        """
        counts = Counter(m.group(0).casefold() for m in _WORD.finditer(text))
        weights: dict[int, float] = {}
        for token, count in counts.items():
            index = int.from_bytes(
                blake2b(token.encode("utf-8"), digest_size=4).digest(), "big"
            )
            weights[index] = weights.get(index, 0.0) + float(count)
        indices = sorted(weights)
        return indices, [weights[index] for index in indices]

    @staticmethod
    def _item_to_payload(item: MemoryItem, *, include_text: bool = True) -> dict[str, object]:
        """Serialize a MemoryItem into a Qdrant-compatible payload dict."""
        payload: dict[str, object] = {
            "memory_id": str(item.id),
            "tenant_id": str(item.tenant_id),
            "workspace_id": str(item.workspace_id),
            "agent_id": str(item.agent_id) if item.agent_id else None,
            "thread_id": str(item.thread_id) if item.thread_id else None,
            "layer": str(item.layer),
            "scope": str(item.scope),
            "kind": item.kind,
            "labels": list(item.labels),
            "status": item.status.value,
            "importance": item.importance,
            "salience": item.salience,
            "confidence": item.confidence,
            "created_at": item.created_at.isoformat(),
            "revision": item.revision,
            "supersedes_id": str(item.supersedes_id) if item.supersedes_id else None,
        }
        if include_text:
            payload["text"] = item.text
        else:
            payload["text_redacted"] = True
        return payload

    def _payload_to_candidate_item(self, payload: dict[str, object]) -> MemoryItem:
        """Hydrate a redacted Qdrant payload from the ledger when available."""
        if not payload.get("text") and self._ledger is not None:
            memory_id = UUID(str(payload["memory_id"]))
            tenant_id = UUID(str(payload["tenant_id"]))
            hydrated = self._ledger.get(tenant_id, memory_id)
            if hydrated is not None:
                return hydrated
        item = self._payload_to_item(payload)
        return item

    @staticmethod
    def _payload_to_item(payload: dict[str, object]) -> MemoryItem:
        """Reconstruct the memory item shape needed for retrieval fusion."""
        labels_raw = payload.get("labels") or []
        labels = tuple(str(label) for label in labels_raw) if isinstance(labels_raw, list) else ()
        supersedes_raw = payload.get("supersedes_id")
        created_raw = payload.get("created_at")
        created_at = (
            datetime.fromisoformat(str(created_raw)) if created_raw else datetime.now().astimezone()
        )
        return MemoryItem(
            id=UUID(str(payload["memory_id"])),
            tenant_id=UUID(str(payload["tenant_id"])),
            workspace_id=UUID(str(payload["workspace_id"])),
            agent_id=UUID(str(payload["agent_id"])) if payload.get("agent_id") else None,
            thread_id=UUID(str(payload["thread_id"])) if payload.get("thread_id") else None,
            layer=MemoryLayer(str(payload["layer"])),
            scope=MemoryScope(str(payload["scope"])),
            kind=str(payload.get("kind") or "fact"),
            text=str(payload.get("text") or ""),
            labels=labels,
            status=MemoryStatus(str(payload.get("status") or MemoryStatus.ACTIVE)),
            importance=float(cast(_NumericPayload, payload.get("importance") or 0.5)),
            salience=float(cast(_NumericPayload, payload.get("salience") or 0.5)),
            confidence=float(cast(_NumericPayload, payload.get("confidence") or 0.7)),
            created_at=created_at,
            revision=int(cast(_NumericPayload, payload.get("revision") or 1)),
            supersedes_id=UUID(str(supersedes_raw)) if supersedes_raw else None,
            provenance=Provenance(source_kind="qdrant-payload"),
        )

    @staticmethod
    def _bounded_cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = sqrt(sum(a * a for a in left))
        right_norm = sqrt(sum(b * b for b in right))
        cosine = dot / max(left_norm * right_norm, 1e-12)
        return max(0.0, min(1.0, (cosine + 1.0) / 2.0))

    def _embed_query(self, text: str) -> list[float]:
        """Use query-specific embeddings when the provider exposes them."""
        assert self._query_embedding_client is not None
        embed_query = getattr(self._query_embedding_client, "embed_query", None)
        if callable(embed_query):
            return cast(_QueryEmbeddingMethod, embed_query)(text)
        return self._query_embedding_client.embed(text)

    def _query_points(
        self,
        *,
        query_vector: list[float],
        query_filter: object,
        limit: int,
    ) -> Sequence[_ScoredPoint]:
        """Run a named-vector query across qdrant-client API versions."""
        assert self._client is not None
        query_points = getattr(self._client, "query_points", None)
        if callable(query_points):
            response = cast(_QueryPointsClient, self._client).query_points(
                collection_name=self.collection,
                query=query_vector,
                using="dense",
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            return response.points
        search = getattr(self._client, "search", None)
        if callable(search):
            return cast(_SearchMethod, search)(
                collection_name=self.collection,
                query_vector=("dense", query_vector),
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
        raise RuntimeError("qdrant client exposes neither query_points nor search")

    def _query_hybrid_points(
        self,
        *,
        query_vector: list[float],
        sparse_indices: list[int],
        sparse_values: list[float],
        query_filter: object,
        limit: int,
    ) -> Sequence[_ScoredPoint]:
        """Fuse dense and sparse candidates with RRF when the client supports it."""
        assert self._client is not None
        query_points = getattr(self._client, "query_points", None)
        if not callable(query_points):
            return self._query_points(
                query_vector=query_vector, query_filter=query_filter, limit=limit
            )
        try:
            from qdrant_client.models import Fusion, FusionQuery, Prefetch, SparseVector
        except ImportError:
            return self._query_points(
                query_vector=query_vector, query_filter=query_filter, limit=limit
            )
        try:
            response = cast(_QueryPointsClient, self._client).query_points(
                collection_name=self.collection,
                prefetch=[
                    Prefetch(query=query_vector, using="dense", limit=limit),
                    Prefetch(
                        query=SparseVector(indices=sparse_indices, values=sparse_values),
                        using="sparse",
                        limit=limit,
                    ),
                ],
                query=FusionQuery(fusion=Fusion.RRF),
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            return response.points
        except TypeError:
            # Older qdrant-client versions expose query_points but do not yet
            # implement fusion. Preserve availability rather than failing recall.
            return self._query_points(
                query_vector=query_vector, query_filter=query_filter, limit=limit
            )
