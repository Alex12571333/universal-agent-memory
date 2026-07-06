"""Composition root: the only place where concrete adapters meet services."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from memory_plane.adapters.in_memory import (
    InMemoryCheckpointStore,
    InMemoryMemoryStore,
    InMemoryObservationRepository,
)
from memory_plane.adapters.postgres import (
    PostgresCheckpointStore,
    PostgresMemoryLedger,
    PostgresObservationRepository,
)
from memory_plane.adapters.qdrant import QdrantCandidateSource
from memory_plane.services.checkpoint import CheckpointService
from memory_plane.services.context import ContextCompiler
from memory_plane.services.embedding import EmbeddingService
from memory_plane.services.ingestion import IngestionService
from memory_plane.services.reflection import ReflectionService
from memory_plane.services.retention import RetentionService
from memory_plane.services.retrieval import RetrievalService
from memory_plane.services.vault import VaultExporter


@dataclass(frozen=True, slots=True)
class Container:
    """Explicit service graph passed to API, workers and tests."""

    retention: RetentionService
    ingestion: IngestionService
    retrieval: RetrievalService
    context: ContextCompiler
    reflection: ReflectionService
    checkpoint: CheckpointService
    embedding: EmbeddingService
    vault: VaultExporter
    store: object


def build_in_memory_container() -> Container:
    """Build a zero-infrastructure container for development and contract tests."""
    store = InMemoryMemoryStore()
    retention = RetentionService(store)
    from memory_plane.adapters.embeddings import FakeEmbeddingClient

    qdrant = QdrantCandidateSource(url="http://localhost:6333", dense_dim=1536)
    qdrant._use_in_memory_backend()
    client = FakeEmbeddingClient()
    embedding = EmbeddingService(store, qdrant, client)
    observations = InMemoryObservationRepository(store)

    return Container(
        retention=retention,
        ingestion=IngestionService(retention),
        retrieval=RetrievalService((store, qdrant)),
        context=ContextCompiler(),
        reflection=ReflectionService(store, observations),
        checkpoint=CheckpointService(InMemoryCheckpointStore()),
        embedding=embedding,
        vault=VaultExporter(store, observations),
        store=store,
    )


def build_postgres_container(
    dsn: str,
    *,
    server_id: UUID,
    project_id: UUID,
    qdrant_url: str | None = None,
    qdrant_dim: int = 1536,
) -> Container:
    """Build the durable single-server graph used by the Docker image."""
    store = PostgresMemoryLedger(dsn)
    store.connect()
    store.ensure_standalone_scope(server_id, project_id)
    retention = RetentionService(store)
    observations = PostgresObservationRepository(store)

    import os

    from memory_plane.adapters.embeddings import FakeEmbeddingClient

    # Assemble candidate sources: always include PostgreSQL lexical; optionally
    # add Qdrant for dense+sparse hybrid retrieval.
    from memory_plane.ports.repositories import CandidateSource

    sources: list[CandidateSource] = [store]
    qdrant_url_val = qdrant_url or os.getenv("UAM_QDRANT_URL")
    if qdrant_url_val:
        qdrant = QdrantCandidateSource(
            url=qdrant_url_val,
            dense_dim=qdrant_dim,
        )
        qdrant.connect()
        sources.append(qdrant)
    else:
        qdrant = QdrantCandidateSource(
            url="http://localhost:6333",
            dense_dim=qdrant_dim,
        )
        qdrant._use_in_memory_backend()

    client = FakeEmbeddingClient(
        model_name=os.getenv("UAM_EMBEDDING_MODEL", "fake-embed-v1"),
        dimension=qdrant_dim,
    )
    embedding = EmbeddingService(store, qdrant, client)

    return Container(
        retention=retention,
        ingestion=IngestionService(retention),
        retrieval=RetrievalService(tuple(sources)),
        context=ContextCompiler(),
        reflection=ReflectionService(store, observations),
        checkpoint=CheckpointService(PostgresCheckpointStore(store)),
        embedding=embedding,
        vault=VaultExporter(store, observations),
        store=store,
    )
