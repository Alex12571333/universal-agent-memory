"""FastAPI application factory; imports remain optional for core users."""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from typing import Any, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from memory_plane.adapters.documents import BinaryDocumentCommand, DocumentIngestor
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
    SupersedeMemoryCommand,
)
from memory_plane.domain.checkpoint import Checkpoint, StaleRevisionError
from memory_plane.domain.models import (
    MemoryLayer,
    MemoryRevisionConflictError,
    MemoryScope,
    Provenance,
)

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


class SupersedeMemoryBody(BaseModel):
    """CAS request for replacing one memory head with a new immutable revision."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    text: str
    expected_revision: int = Field(ge=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    idempotency_key: str | None = None


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


class CheckpointSaveBody(BaseModel):
    """Save a new working-memory checkpoint."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    workspace_id: UUID = DEFAULT_PROJECT_ID
    thread_id: UUID
    state: dict[str, Any]


class CheckpointUpdateBody(BaseModel):
    """CAS-update an existing checkpoint."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    workspace_id: UUID = DEFAULT_PROJECT_ID
    state: dict[str, Any]
    expected_revision: int = Field(ge=1)


class CheckpointCompactBody(BaseModel):
    """Compaction request body."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    keep_last: int = Field(default=3, ge=1)


def _checkpoint_response(cp: Checkpoint) -> dict[str, Any]:
    """Render a checkpoint as a JSON-compatible dict."""
    return {
        "id": str(cp.id),
        "tenant_id": str(cp.tenant_id),
        "workspace_id": str(cp.workspace_id),
        "thread_id": str(cp.thread_id),
        "revision": cp.revision,
        "state": cp.state,
        "created_at": cp.created_at.isoformat(),
    }


def _memory_write_response(result: Any) -> dict[str, Any]:
    """Render a write result with revision metadata needed for CAS clients."""
    return {
        "id": str(result.item.id),
        "created": result.created,
        "revision": result.item.revision,
        "supersedes_id": (
            str(result.item.supersedes_id)
            if result.item.supersedes_id is not None
            else None
        ),
        "queued_event_ids": [str(event_id) for event_id in result.queued_event_ids],
    }


class IngestDocumentBody(BaseModel):
    """Base64 Markdown/PDF ingestion request."""

    content_base64: str
    format: Literal["markdown", "pdf"]
    origin_uri: str
    tenant_id: UUID = DEFAULT_SERVER_ID
    workspace_id: UUID = DEFAULT_PROJECT_ID
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    labels: list[str] = Field(default_factory=list)
    chunk_size_chars: int = Field(default=2400, ge=256)
    chunk_overlap_chars: int = Field(default=240, ge=0)


def create_app(
    container: Container | None = None,
    *,
    api_key: str | None = None,
) -> FastAPI:
    """Create the standalone memory server around an injected service graph."""
    services = container or _build_runtime_container()
    documents = DocumentIngestor(services.ingestion)
    configured_key = api_key if api_key is not None else os.getenv("UAM_API_KEY")
    app = FastAPI(
        title="Universal Agent Memory Server",
        version="0.1.0",
        description="Self-hosted memory API for local and team AI agents.",
    )

    @app.middleware("http")
    async def require_api_key(request: Request, call_next: Any) -> Any:
        """Protect every endpoint except liveness when a server key is configured."""
        if not configured_key or request.url.path == "/health":
            return await call_next(request)
        authorization = request.headers.get("Authorization", "")
        scheme, _, credential = authorization.partition(" ")
        credential_matches = secrets.compare_digest(credential, configured_key)
        valid = scheme.casefold() == "bearer" and credential_matches
        if not valid:
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or missing API key"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

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
        return _memory_write_response(result)

    @app.put("/v1/memory/{item_id}/supersede", status_code=201)
    def supersede_memory(item_id: UUID, body: SupersedeMemoryBody) -> dict[str, Any]:
        """Append a replacement only when the caller's revision is still current."""
        try:
            result = services.retention.supersede(
                SupersedeMemoryCommand(
                    tenant_id=body.tenant_id,
                    item_id=item_id,
                    replacement_text=body.text,
                    expected_revision=body.expected_revision,
                    confidence=body.confidence,
                    idempotency_key=body.idempotency_key,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="memory item not found") from exc
        except MemoryRevisionConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "revision_conflict",
                    "message": str(exc),
                    "expected": exc.expected,
                    "actual": exc.actual,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _memory_write_response(result)

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

    @app.post("/v1/ingest/document", status_code=202)
    def ingest_document(body: IngestDocumentBody) -> dict[str, Any]:
        """Decode and ingest a Markdown or text-bearing PDF document."""
        try:
            data = base64.b64decode(body.content_base64, validate=True)
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("document exceeds 20 MiB limit")
            command = BinaryDocumentCommand(
                tenant_id=body.tenant_id,
                workspace_id=body.workspace_id,
                data=data,
                origin_uri=body.origin_uri,
                agent_id=body.agent_id,
                thread_id=body.thread_id,
                labels=tuple(body.labels),
                chunk_size_chars=body.chunk_size_chars,
                chunk_overlap_chars=body.chunk_overlap_chars,
            )
            result = (
                documents.ingest_markdown(command)
                if body.format == "markdown"
                else documents.ingest_pdf(command)
            )
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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

    @app.post("/v1/workspaces/{workspace_id}/reindex", status_code=202)
    def reindex(workspace_id: UUID, tenant_id: UUID) -> dict[str, Any]:
        """Re-generate all embeddings for the workspace."""
        count = services.embedding.reindex_all(tenant_id, workspace_id)
        return {"reindexed_count": count}

    # ── Checkpoint endpoints ────────────────────────────────────────

    @app.post("/v1/checkpoints", status_code=201)
    def save_checkpoint(body: CheckpointSaveBody) -> dict[str, Any]:
        """Save a new working-memory checkpoint revision."""
        try:
            cp = services.checkpoint.save(
                tenant_id=body.tenant_id,
                workspace_id=body.workspace_id,
                thread_id=body.thread_id,
                state=body.state,
            )
        except StaleRevisionError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "revision_conflict",
                    "message": str(exc),
                    "expected": exc.expected,
                    "actual": exc.actual,
                },
            ) from exc
        return _checkpoint_response(cp)

    @app.get("/v1/checkpoints")
    def list_checkpoints(
        workspace_id: UUID = DEFAULT_PROJECT_ID,
        tenant_id: UUID = DEFAULT_SERVER_ID,
    ) -> list[dict[str, Any]]:
        """List head checkpoints for all threads in a workspace."""
        heads = services.checkpoint.list_for_workspace(tenant_id, workspace_id)
        return [_checkpoint_response(cp) for cp in heads]

    @app.get("/v1/checkpoints/{thread_id}")
    def restore_checkpoint(
        thread_id: UUID,
        tenant_id: UUID = DEFAULT_SERVER_ID,
    ) -> dict[str, Any]:
        """Restore the latest checkpoint for a thread."""
        cp = services.checkpoint.restore(tenant_id=tenant_id, thread_id=thread_id)
        if cp is None:
            raise HTTPException(404, "checkpoint not found")
        return _checkpoint_response(cp)

    @app.get("/v1/checkpoints/{thread_id}/revisions/{revision}")
    def restore_checkpoint_revision(
        thread_id: UUID,
        revision: int,
        tenant_id: UUID = DEFAULT_SERVER_ID,
    ) -> dict[str, Any]:
        """Restore a specific historical checkpoint revision."""
        cp = services.checkpoint.restore_revision(
            tenant_id=tenant_id, thread_id=thread_id, revision=revision
        )
        if cp is None:
            raise HTTPException(404, "checkpoint revision not found")
        return _checkpoint_response(cp)

    @app.put("/v1/checkpoints/{thread_id}")
    def update_checkpoint(
        thread_id: UUID, body: CheckpointUpdateBody
    ) -> dict[str, Any]:
        """CAS-update a checkpoint; returns 409 on stale revision."""
        try:
            cp = services.checkpoint.update(
                tenant_id=body.tenant_id,
                workspace_id=body.workspace_id,
                thread_id=thread_id,
                state=body.state,
                expected_revision=body.expected_revision,
            )
        except StaleRevisionError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "revision_conflict",
                    "message": str(exc),
                    "expected": exc.expected,
                    "actual": exc.actual,
                },
            ) from exc
        return _checkpoint_response(cp)

    @app.post("/v1/checkpoints/{thread_id}/compact")
    def compact_checkpoint(
        thread_id: UUID, body: CheckpointCompactBody
    ) -> dict[str, Any]:
        """Delete old revisions keeping the most recent *keep_last*."""
        deleted = services.checkpoint.compact(
            tenant_id=body.tenant_id,
            thread_id=thread_id,
            keep_last=body.keep_last,
        )
        return {"deleted": deleted}

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
