"""Composition root: the only place where concrete adapters meet services."""

from __future__ import annotations

from dataclasses import dataclass

from memory_plane.adapters.in_memory import (
    InMemoryMemoryStore,
    InMemoryObservationRepository,
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
    store: InMemoryMemoryStore


def build_in_memory_container() -> Container:
    """Build a zero-infrastructure container for development and contract tests."""
    store = InMemoryMemoryStore()
    retention = RetentionService(store, store)
    return Container(
        retention=retention,
        ingestion=IngestionService(retention),
        retrieval=RetrievalService((store,)),
        context=ContextCompiler(),
        reflection=ReflectionService(store, InMemoryObservationRepository(store)),
        store=store,
    )
