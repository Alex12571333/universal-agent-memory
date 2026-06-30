"""FastAPI application factory; imports remain optional for core users."""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from memory_plane.bootstrap import (
    Container,
    build_in_memory_container,
    build_postgres_container,
)
from memory_plane.contracts.dto import (
    ContextRecipe,
    IngestDocumentCommand,
    RecallQuery,
    RetainCommand,
)
from memory_plane.domain.models import MemoryLayer, MemoryScope, Provenance

DEFAULT_SERVER_ID = UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")


class RetainBody(BaseModel):
    """External retain request schema."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    workspace_id: UUID = DEFAULT_PROJECT_ID
    layer: MemoryLayer
    scope: MemoryScope
    kind: str
    text: str
    source_kind: str = "api"
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    labels: list[str] = Field(default_factory=list)
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.7, ge=0, le=1)
    idempotency_key: str | None = None


class RecallBody(BaseModel):
    """External recall request schema."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    workspace_id: UUID = DEFAULT_PROJECT_ID
    query: str
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    layers: list[MemoryLayer] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    top_k: int = Field(default=12, ge=1, le=100)
    minimum_score: float = Field(default=0, ge=0, le=1)
    operation: str = "chat_reply"
    context_budget_tokens: int = Field(default=4000, ge=128)


class IngestTextBody(BaseModel):
    """External normalized-text ingestion request."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    workspace_id: UUID = DEFAULT_PROJECT_ID
    text: str
    origin_uri: str
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    labels: list[str] = Field(default_factory=list)
    chunk_size_chars: int = Field(default=2400, ge=256)
    chunk_overlap_chars: int = Field(default=240, ge=0)


def create_app(container: Container | None = None) -> FastAPI:
    """Create the standalone memory server around an injected service graph."""
    services = container or _build_runtime_container()
    app = FastAPI(
        title="Universal Agent Memory Server",
        version="0.1.0",
        description="Self-hosted memory API for local and team AI agents.",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        """Report process liveness; adapters should extend readiness separately."""
        return {"status": "ok"}

    @app.post("/v1/memory/retain", status_code=201)
    def retain(body: RetainBody) -> dict[str, Any]:
        """Append memory and return its canonical identity and outbox status."""
        try:
            result = services.retention.retain(
                RetainCommand(
                    tenant_id=body.tenant_id,
                    workspace_id=body.workspace_id,
                    layer=body.layer,
                    scope=body.scope,
                    kind=body.kind,
                    text=body.text,
                    provenance=Provenance(source_kind=body.source_kind),
                    agent_id=body.agent_id,
                    thread_id=body.thread_id,
                    labels=tuple(body.labels),
                    importance=body.importance,
                    confidence=body.confidence,
                    idempotency_key=body.idempotency_key,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "id": str(result.item.id),
            "created": result.created,
            "queued_event_ids": [str(event_id) for event_id in result.queued_event_ids],
        }

    @app.post("/v1/memory/recall")
    def recall(body: RecallBody) -> dict[str, Any]:
        """Run hybrid recall and compile an operation-specific context package."""
        query = RecallQuery(
            tenant_id=body.tenant_id,
            workspace_id=body.workspace_id,
            text=body.query,
            agent_id=body.agent_id,
            thread_id=body.thread_id,
            layers=tuple(body.layers),
            labels=tuple(body.labels),
            top_k=body.top_k,
            minimum_score=body.minimum_score,
        )
        result = services.retrieval.recall(query)
        recipe = ContextRecipe(
            operation=body.operation,
            budget_tokens=body.context_budget_tokens,
            layer_order=(
                MemoryLayer.SEMANTIC,
                MemoryLayer.REFLECTION,
                MemoryLayer.PROCEDURAL,
                MemoryLayer.EPISODIC,
                MemoryLayer.ERROR,
                MemoryLayer.SOCIAL,
            ),
        )
        package = services.context.compile(result, recipe)
        return {
            "results": [
                {
                    "id": str(row.item.id),
                    "text": row.item.text,
                    "layer": row.item.layer.value,
                    "score": row.final_score,
                    "source": row.source,
                }
                for row in result.candidates
            ],
            "sources_used": result.sources_used,
            "context": {
                "operation": package.operation,
                "used_tokens": package.used_tokens,
                "budget_tokens": package.budget_tokens,
                "markdown": package.render_markdown(),
                "trace_ids": [str(item_id) for item_id in package.trace_ids],
            },
        }

    @app.post("/v1/ingest/text", status_code=202)
    def ingest_text(body: IngestTextBody) -> dict[str, Any]:
        """Ingest normalized text; binary parsers belong in independent adapters."""
        try:
            result = services.ingestion.ingest_text(
                IngestDocumentCommand(
                    tenant_id=body.tenant_id,
                    workspace_id=body.workspace_id,
                    text=body.text,
                    origin_uri=body.origin_uri,
                    agent_id=body.agent_id,
                    thread_id=body.thread_id,
                    labels=tuple(body.labels),
                    chunk_size_chars=body.chunk_size_chars,
                    chunk_overlap_chars=body.chunk_overlap_chars,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "document_checksum": result.document_checksum,
            "memory_ids": [str(item_id) for item_id in result.memory_ids],
            "created_count": result.created_count,
        }

    @app.post("/v1/workspaces/{workspace_id}/reflect", status_code=202)
    def reflect(workspace_id: UUID, tenant_id: UUID) -> dict[str, Any]:
        """Run the baseline reflection synchronously behind an async-shaped API."""
        observations = services.reflection.reflect(tenant_id, workspace_id)
        return {
            "created": len(observations),
            "observation_ids": [str(row.id) for row in observations],
        }

    return app


def _build_runtime_container() -> Container:
    """Select durable Docker mode when a database URL is configured."""
    dsn = os.getenv("UAM_DATABASE_URL")
    if not dsn:
        return build_in_memory_container()
    server_id = UUID(os.getenv("UAM_SERVER_ID", str(DEFAULT_SERVER_ID)))
    project_id = UUID(os.getenv("UAM_PROJECT_ID", str(DEFAULT_PROJECT_ID)))
    qdrant_url = os.getenv("UAM_QDRANT_URL")
    qdrant_dim = int(os.getenv("UAM_EMBEDDING_DIM", "1536"))
    return build_postgres_container(
        dsn,
        server_id=server_id,
        project_id=project_id,
        qdrant_url=qdrant_url,
        qdrant_dim=qdrant_dim,
    )
