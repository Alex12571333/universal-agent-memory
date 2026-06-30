"""Composition root: the only place where concrete adapters meet services."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from memory_plane.adapters.in_memory import (
    InMemoryMemoryStore,
    InMemoryObservationRepository,
)
from memory_plane.adapters.postgres import (
    PostgresMemoryLedger,
    PostgresObservationRepository,
)
from memory_plane.services.context import ContextCompiler
from memory_plane.services.ingestion import IngestionService
from memory_plane.services.reflection import ReflectionService
from memory_plane.services.retention import RetentionService
from memory_plane.services.retrieval import RetrievalService


@dataclass(frozen=True, slots=True)
class Container:
    """Explicit service graph passed to API, workers and tests."""

    retention: RetentionService
    ingestion: IngestionService
    retrieval: RetrievalService
    context: ContextCompiler
    reflection: ReflectionService
    store: object


def build_in_memory_container() -> Container:
    """Build a zero-infrastructure container for development and contract tests."""
    store = InMemoryMemoryStore()
    retention = RetentionService(store)
    return Container(
        retention=retention,
        ingestion=IngestionService(retention),
        retrieval=RetrievalService((store,)),
        context=ContextCompiler(),
        reflection=ReflectionService(store, InMemoryObservationRepository(store)),
        store=store,
    )


def build_postgres_container(
    dsn: str,
    *,
    server_id: UUID,
    project_id: UUID,
) -> Container:
    """Build the durable single-server graph used by the Docker image."""
    store = PostgresMemoryLedger(dsn)
    store.connect()
    store.ensure_standalone_scope(server_id, project_id)
    retention = RetentionService(store)
    return Container(
        retention=retention,
        ingestion=IngestionService(retention),
        retrieval=RetrievalService((store,)),
        context=ContextCompiler(),
        reflection=ReflectionService(store, PostgresObservationRepository(store)),
        store=store,
    )
