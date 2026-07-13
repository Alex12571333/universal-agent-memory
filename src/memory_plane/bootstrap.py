"""Composition root: the only place where concrete adapters meet services."""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID

from memory_plane.adapters.in_memory import (
    InMemoryCheckpointStore,
    InMemoryConflictReviewRepository,
    InMemoryGraphRepository,
    InMemoryMemoryStore,
    InMemoryObservationRepository,
)
from memory_plane.adapters.llm import MemoryLLMClient, build_memory_llm_client
from memory_plane.adapters.postgres import (
    PostgresCheckpointStore,
    PostgresConflictReviewRepository,
    PostgresGraphRepository,
    PostgresMemoryLedger,
    PostgresObservationRepository,
)
from memory_plane.adapters.qdrant import QdrantCandidateSource
from memory_plane.services.api_keys import ApiKeyRegistryService
from memory_plane.services.audit import AuditLogService
from memory_plane.services.checkpoint import CheckpointService
from memory_plane.services.conflicts import ConflictService
from memory_plane.services.context import ContextCompiler
from memory_plane.services.conversations import ConversationCurator, ConversationService
from memory_plane.services.embedding import EmbeddingService
from memory_plane.services.graph import GraphService
from memory_plane.services.identities import IdentityProvisioningService
from memory_plane.services.ingestion import IngestionService
from memory_plane.services.proposals import MemoryProposalService
from memory_plane.services.reflection import ReflectionService
from memory_plane.services.replay import RecallReplayService
from memory_plane.services.retention import RetentionService
from memory_plane.services.retrieval import RetrievalService
from memory_plane.services.session_seed import SessionSeedService
from memory_plane.services.vault import VaultExporter
from memory_plane.services.vault_health import VaultHealthService


@dataclass(frozen=True, slots=True)
class Container:
    """Explicit service graph passed to API, workers and tests."""

    retention: RetentionService
    ingestion: IngestionService
    identities: IdentityProvisioningService
    retrieval: RetrievalService
    context: ContextCompiler
    reflection: ReflectionService
    conflicts: ConflictService
    graph: GraphService
    checkpoint: CheckpointService
    conversations: ConversationService
    curator: ConversationCurator
    proposals: MemoryProposalService
    audit: AuditLogService
    api_keys: ApiKeyRegistryService
    embedding: EmbeddingService
    memory_llm: MemoryLLMClient
    vault: VaultExporter
    vault_health: VaultHealthService
    replay: RecallReplayService
    session_seed: SessionSeedService
    store: object


def build_in_memory_container() -> Container:
    """Build a zero-infrastructure container for development and contract tests."""
    store = InMemoryMemoryStore()
    retention = RetentionService(store)
    from memory_plane.adapters.embeddings import FakeEmbeddingClient

    client = FakeEmbeddingClient()
    qdrant = QdrantCandidateSource(
        url="http://localhost:6333",
        dense_dim=1536,
        query_embedding_client=client,
        ledger=store,
    )
    qdrant._use_in_memory_backend()
    embedding = EmbeddingService(store, qdrant, client)
    retrieval = RetrievalService(
        (store, qdrant),
        staleness_check=lambda query: _workspace_embedding_freshness(
            store, query.tenant_id, query.workspace_id
        ),
    )
    retrieval.record_success(store.name)
    retrieval.record_success(qdrant.name)
    observations = InMemoryObservationRepository(store)
    conflict_reviews = InMemoryConflictReviewRepository(store)
    graph = InMemoryGraphRepository(store)

    proposals = MemoryProposalService(store, retention)
    return Container(
        retention=retention,
        ingestion=IngestionService(retention),
        identities=IdentityProvisioningService(store),
        retrieval=retrieval,
        context=ContextCompiler(),
        reflection=ReflectionService(store, observations),
        conflicts=ConflictService(store, conflict_reviews),
        graph=GraphService(store, graph),
        checkpoint=CheckpointService(InMemoryCheckpointStore()),
        conversations=ConversationService(store),
        curator=ConversationCurator(store, retention, proposals=proposals),
        proposals=proposals,
        audit=AuditLogService(store),
        api_keys=ApiKeyRegistryService(store),
        embedding=embedding,
        memory_llm=build_memory_llm_client(),
        vault=VaultExporter(store, observations, retention),
        vault_health=VaultHealthService(store, observations, graph),
        replay=RecallReplayService(AuditLogService(store), store),
        session_seed=SessionSeedService(store),
        store=store,
    )


def build_postgres_container(
    dsn: str,
    *,
    server_id: UUID,
    project_id: UUID,
    qdrant_url: str | None = None,
    qdrant_dim: int = 1536,
    qdrant_collection: str | None = None,
    require_qdrant: bool = False,
) -> Container:
    """Build the durable single-server graph used by the Docker image."""
    store = PostgresMemoryLedger(dsn)
    store.connect()
    store.ensure_standalone_scope(server_id, project_id)
    retention = RetentionService(store)
    observations = PostgresObservationRepository(store)
    conflict_reviews = PostgresConflictReviewRepository(store)
    graph = PostgresGraphRepository(store)

    import os

    from memory_plane.adapters.embeddings import EmbeddingProviderConfig, build_embedding_client

    embedding_config = replace(EmbeddingProviderConfig.from_env(), dimension=qdrant_dim)
    client = build_embedding_client(embedding_config)

    # Assemble candidate sources: always include PostgreSQL lexical; optionally
    # add Qdrant for dense+sparse hybrid retrieval.
    from memory_plane.ports.repositories import CandidateSource

    sources: list[CandidateSource] = [store]
    qdrant_url_val = qdrant_url or os.getenv("UAM_QDRANT_URL")
    qdrant_collection_val = (
        qdrant_collection or os.getenv("UAM_QDRANT_COLLECTION") or "memory_items"
    )
    qdrant_payload_text = _env_bool("UAM_QDRANT_PAYLOAD_TEXT", default=True)
    if qdrant_url_val:
        qdrant = QdrantCandidateSource(
            url=qdrant_url_val,
            collection=qdrant_collection_val,
            dense_dim=qdrant_dim,
            query_embedding_client=client,
            ledger=store,
            payload_text=qdrant_payload_text,
        )
        qdrant_error: Exception | None = None
        try:
            qdrant.connect()
        except Exception as exc:
            if require_qdrant:
                raise
            qdrant_error = exc
        sources.append(qdrant)
    else:
        qdrant = QdrantCandidateSource(
            url="http://localhost:6333",
            collection=qdrant_collection_val,
            dense_dim=qdrant_dim,
            query_embedding_client=client,
            ledger=store,
            payload_text=qdrant_payload_text,
        )
        qdrant._use_in_memory_backend()

    retrieval = RetrievalService(
        tuple(sources),
        required_sources=frozenset({store.name}),
        staleness_check=lambda query: _workspace_embedding_freshness(
            store, query.tenant_id, query.workspace_id
        ),
    )
    retrieval.record_success(store.name)
    if qdrant_url_val:
        if qdrant_error is None:
            retrieval.record_success(qdrant.name)
        else:
            retrieval.record_failure(qdrant.name, qdrant_error)

    embedding = EmbeddingService(store, qdrant, client)
    memory_llm = build_memory_llm_client()

    proposals = MemoryProposalService(store, retention, memory_llm=memory_llm)
    return Container(
        retention=retention,
        ingestion=IngestionService(retention),
        identities=IdentityProvisioningService(store),
        retrieval=retrieval,
        context=ContextCompiler(),
        reflection=ReflectionService(store, observations),
        conflicts=ConflictService(store, conflict_reviews),
        graph=GraphService(store, graph),
        checkpoint=CheckpointService(PostgresCheckpointStore(store)),
        conversations=ConversationService(store),
        curator=ConversationCurator(
            store,
            retention,
            memory_llm=memory_llm,
            proposals=proposals,
        ),
        proposals=proposals,
        audit=AuditLogService(store),
        api_keys=ApiKeyRegistryService(store),
        embedding=embedding,
        memory_llm=memory_llm,
        vault=VaultExporter(store, observations, retention),
        vault_health=VaultHealthService(store, observations, graph),
        replay=RecallReplayService(AuditLogService(store), store),
        session_seed=SessionSeedService(store),
        store=store,
    )


def _workspace_embedding_freshness(store: object, tenant_id: UUID, workspace_id: UUID):
    """Report exact active-head embedding state from the canonical ledger."""
    workspace_probe = getattr(store, "workspace_embedding_freshness", None)
    if callable(workspace_probe):
        return workspace_probe(tenant_id, workspace_id)
    stale_probe = getattr(store, "workspace_embedding_stale", None)
    if callable(stale_probe):
        return bool(stale_probe(tenant_id, workspace_id))
    collector = getattr(store, "collect_metrics", None)
    if not callable(collector):
        raise RuntimeError("outbox freshness metrics unavailable")
    metrics = collector(tenant_id)
    return int(metrics.get("outbox_pending_total", 0)) > 0


def _env_bool(name: str, *, default: bool) -> bool:
    """Parse an opt-in/opt-out boolean environment variable."""
    import os

    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
