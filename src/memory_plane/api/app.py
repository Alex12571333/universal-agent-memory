"""FastAPI application factory; imports remain optional for core users."""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from typing import Any, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
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
from memory_plane.domain.conflict import (
    ConflictCase,
    ConflictReviewDecision,
    ConflictReviewStatus,
)
from memory_plane.domain.graph import MemoryEdge, MemoryEdgeType
from memory_plane.domain.models import (
    MemoryLayer,
    MemoryRevisionConflictError,
    MemoryScope,
    MemoryStatus,
    Provenance,
)
from memory_plane.services.metrics import render_prometheus
from memory_plane.services.vault import VaultImportSource

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
    status: MemoryStatus = MemoryStatus.ACTIVE
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


class VaultImportFileBody(BaseModel):
    """One Markdown file being imported from a human-editable vault."""

    path: str
    content: str


class VaultImportBody(BaseModel):
    """Dry-run or apply a vault import safely through supersede."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    dry_run: bool = True
    files: list[VaultImportFileBody]


class ConflictDecisionBody(BaseModel):
    """Persist a human decision for one conflict case."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    status: ConflictReviewStatus
    winner_value: str | None = None
    reason: str = ""


class GraphEdgeBody(BaseModel):
    """Create one typed graph edge between two memories."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    workspace_id: UUID = DEFAULT_PROJECT_ID
    src_id: UUID
    dst_id: UUID
    edge_type: MemoryEdgeType
    weight: float = Field(default=1.0, ge=0, le=1)
    provenance_item_id: UUID | None = None


def _conflict_case_response(case: ConflictCase) -> dict[str, Any]:
    """Render a conflict case as JSON."""
    return {
        "id": str(case.id),
        "tenant_id": str(case.tenant_id),
        "workspace_id": str(case.workspace_id),
        "subject": case.subject,
        "predicate": case.predicate,
        "review_status": case.review_status.value,
        "suggested_winner_value": case.suggested_winner_value,
        "suggested_reason": case.suggested_reason,
        "review": _conflict_decision_response(case.review) if case.review else None,
        "candidates": [
            {
                "value": candidate.value,
                "status": candidate.status,
                "evidence_ids": [str(item_id) for item_id in candidate.evidence_ids],
                "confidence": candidate.confidence,
                "latest_created_at": candidate.latest_created_at.isoformat(),
            }
            for candidate in case.candidates
        ],
    }


def _conflict_decision_response(
    decision: ConflictReviewDecision | None,
) -> dict[str, Any] | None:
    """Render a persisted conflict review decision."""
    if decision is None:
        return None
    return {
        "tenant_id": str(decision.tenant_id),
        "workspace_id": str(decision.workspace_id),
        "case_id": str(decision.case_id),
        "status": decision.status.value,
        "winner_value": decision.winner_value,
        "reason": decision.reason,
        "updated_at": decision.updated_at.isoformat(),
    }


def _graph_edge_response(edge: MemoryEdge) -> dict[str, Any]:
    """Render graph edge as JSON."""
    return {
        "id": str(edge.id),
        "tenant_id": str(edge.tenant_id),
        "workspace_id": str(edge.workspace_id),
        "src_id": str(edge.src_id),
        "dst_id": str(edge.dst_id),
        "edge_type": edge.edge_type.value,
        "weight": edge.weight,
        "provenance_item_id": (
            str(edge.provenance_item_id) if edge.provenance_item_id else None
        ),
        "created_at": edge.created_at.isoformat(),
    }


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

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(tenant_id: UUID = DEFAULT_SERVER_ID) -> str:
        """Expose core server counters in Prometheus text format."""
        collector = getattr(services.store, "collect_metrics", None)
        if collector is None:
            raise HTTPException(status_code=503, detail="metrics unavailable")
        try:
            rows = collector(tenant_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return render_prometheus(rows)

    @app.get("/ui", response_class=HTMLResponse)
    def operator_ui() -> str:
        """Serve the local human memory console."""
        return _OPERATOR_UI_HTML

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
                    status=body.status,
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
                    "status": row.item.status.value,
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

    @app.get("/v1/workspaces/{workspace_id}/memories")
    def list_memories(
        workspace_id: UUID,
        tenant_id: UUID = DEFAULT_SERVER_ID,
        layer: MemoryLayer | None = None,
        status: MemoryStatus | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """List memory rows for local operator review."""
        layers = (layer,) if layer is not None else ()
        lister = getattr(services.store, "list_for_workspace", None)
        if lister is None:
            raise HTTPException(status_code=503, detail="memory listing unavailable")
        rows = lister(tenant_id, workspace_id, layers=layers)
        if status:
            rows = tuple(row for row in rows if row.status == status)
        if label:
            rows = tuple(row for row in rows if label in row.labels)
        return {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id),
            "count": len(rows),
            "memories": [
                {
                    "id": str(row.id),
                    "layer": row.layer.value,
                    "scope": row.scope.value,
                    "status": row.status.value,
                    "kind": row.kind,
                    "text": row.text,
                    "labels": list(row.labels),
                    "confidence": row.confidence,
                    "revision": row.revision,
                    "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ],
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

    @app.get("/v1/workspaces/{workspace_id}/conflicts")
    def list_conflicts(
        workspace_id: UUID,
        tenant_id: UUID = DEFAULT_SERVER_ID,
        include_resolved: bool = False,
    ) -> dict[str, Any]:
        """List inspectable conflict cases for a workspace."""
        cases = services.conflicts.list_cases(
            tenant_id,
            workspace_id,
            include_resolved=include_resolved,
        )
        return {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id),
            "count": len(cases),
            "cases": [_conflict_case_response(case) for case in cases],
        }

    @app.put("/v1/workspaces/{workspace_id}/conflicts/{case_id}/decision")
    def decide_conflict(
        workspace_id: UUID,
        case_id: UUID,
        body: ConflictDecisionBody,
    ) -> dict[str, Any]:
        """Persist a human/operator decision for one conflict case."""
        try:
            decision = services.conflicts.decide(
                body.tenant_id,
                workspace_id,
                case_id,
                status=body.status,
                winner_value=body.winner_value,
                reason=body.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _conflict_decision_response(decision) or {}

    @app.post("/v1/graph/edges", status_code=201)
    def create_graph_edge(body: GraphEdgeBody) -> dict[str, Any]:
        """Create one typed memory graph edge."""
        try:
            edge = services.graph.link(
                tenant_id=body.tenant_id,
                workspace_id=body.workspace_id,
                src_id=body.src_id,
                dst_id=body.dst_id,
                edge_type=body.edge_type,
                weight=body.weight,
                provenance_item_id=body.provenance_item_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _graph_edge_response(edge)

    @app.get("/v1/memory/{item_id}/neighbors")
    def list_graph_neighbors(
        item_id: UUID,
        workspace_id: UUID = DEFAULT_PROJECT_ID,
        tenant_id: UUID = DEFAULT_SERVER_ID,
        edge_type: MemoryEdgeType | None = None,
    ) -> dict[str, Any]:
        """List incoming and outgoing graph edges for a memory item."""
        edges = services.graph.neighbors(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            item_id=item_id,
            edge_type=edge_type,
        )
        return {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id),
            "item_id": str(item_id),
            "count": len(edges),
            "edges": [_graph_edge_response(edge) for edge in edges],
        }

    @app.post("/v1/workspaces/{workspace_id}/reindex", status_code=202)
    def reindex(workspace_id: UUID, tenant_id: UUID) -> dict[str, Any]:
        """Re-generate all embeddings for the workspace."""
        count = services.embedding.reindex_all(tenant_id, workspace_id)
        return {"reindexed_count": count}

    @app.get("/v1/workspaces/{workspace_id}/vault")
    def export_vault(
        workspace_id: UUID,
        tenant_id: UUID = DEFAULT_SERVER_ID,
    ) -> dict[str, Any]:
        """Export one workspace as deterministic Obsidian-style Markdown files."""
        vault = services.vault.export(tenant_id, workspace_id)
        return {
            "tenant_id": str(vault.tenant_id),
            "workspace_id": str(vault.workspace_id),
            "file_count": len(vault.files),
            "files": [
                {
                    "path": file.path,
                    "content": file.content,
                }
                for file in vault.files
            ],
        }

    @app.post("/v1/workspaces/{workspace_id}/vault/import")
    def import_vault(workspace_id: UUID, body: VaultImportBody) -> dict[str, Any]:
        """Plan or apply a Markdown vault import without destructive overwrites."""
        files = tuple(
            VaultImportSource(path=file.path, content=file.content) for file in body.files
        )
        try:
            result = (
                services.vault.plan_import(body.tenant_id, workspace_id, files)
                if body.dry_run
                else services.vault.apply_import(body.tenant_id, workspace_id, files)
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "tenant_id": str(result.tenant_id),
            "workspace_id": str(result.workspace_id),
            "dry_run": result.dry_run,
            "supersede_count": result.supersede_count,
            "changes": [
                {
                    "path": change.path,
                    "action": change.action,
                    "item_id": str(change.item_id) if change.item_id else None,
                    "expected_revision": change.expected_revision,
                    "new_item_id": str(change.new_item_id) if change.new_item_id else None,
                    "message": change.message,
                }
                for change in result.changes
            ],
        }

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


_OPERATOR_UI_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Universal Agent Memory</title>
  <style>
    :root { color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }
    body { margin: 0; background: #0f1115; color: #e9edf5; }
    header { padding: 24px; border-bottom: 1px solid #2a2f3a; background: #151924; }
    main { padding: 24px; display: grid; gap: 20px; grid-template-columns: 1.4fr .9fr; }
    section { border: 1px solid #2a2f3a; border-radius: 14px; background: #151924; padding: 18px; }
    input, select, textarea, button {
      border: 1px solid #394050; border-radius: 10px; padding: 10px;
      background: #0f1115; color: #e9edf5;
    }
    button { cursor: pointer; background: #2d6cdf; border-color: #2d6cdf; }
    button.secondary { background: #232938; border-color: #394050; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
    .card { border-top: 1px solid #2a2f3a; padding: 12px 0; }
    .muted { color: #9aa4b2; font-size: 13px; }
    .pill {
      display: inline-block; padding: 2px 8px; border-radius: 999px;
      background: #263044; margin-right: 6px;
    }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; }
    @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>Universal Agent Memory</h1>
    <p class="muted">Local operator console. All edits should go through append/supersede APIs.</p>
  </header>
  <main>
    <section>
      <h2>Memory search/list</h2>
      <div class="row">
        <input id="tenant" placeholder="tenant_id" value="00000000-0000-0000-0000-000000000001">
        <input id="workspace" placeholder="workspace_id"
          value="00000000-0000-0000-0000-000000000002">
      </div>
      <div class="row">
        <input id="query" placeholder="semantic recall query">
        <select id="layer">
          <option value="">all layers</option>
          <option>core</option><option>working</option><option>semantic</option>
          <option>episodic</option><option>procedural</option><option>social</option>
          <option>reflection</option><option>error</option>
        </select>
        <button onclick="listMemories()">List</button>
        <button onclick="recall()">Recall</button>
      </div>
      <div id="memories"></div>
    </section>
    <section>
      <h2>Review / ops</h2>
      <div class="row">
        <button onclick="loadConflicts()">Conflict inbox</button>
        <button class="secondary" onclick="reflect()">Reflect</button>
        <button class="secondary" onclick="reindex()">Reindex</button>
      </div>
      <div id="ops"></div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const tenant = () => $("tenant").value.trim();
    const workspace = () => $("workspace").value.trim();

    async function api(path, options = {}) {
      const res = await fetch(path, {
        ...options,
        headers: { "content-type": "application/json", ...(options.headers || {}) },
      });
      const text = await res.text();
      const data = text ? JSON.parse(text) : {};
      if (!res.ok) throw new Error(JSON.stringify(data));
      return data;
    }

    function memoryCard(row) {
      return `<div class="card">
        <div><span class="pill">${row.layer}</span><span class="pill">${row.kind}</span>
        <span class="muted">rev ${row.revision} · confidence ${row.confidence}</span></div>
        <pre>${escapeHtml(row.text)}</pre>
        <div class="muted">${row.id}</div>
      </div>`;
    }

    async function listMemories() {
      const params = new URLSearchParams({ tenant_id: tenant() });
      if ($("layer").value) params.set("layer", $("layer").value);
      const data = await api(`/v1/workspaces/${workspace()}/memories?${params}`);
      $("memories").innerHTML = `<p class="muted">${data.count} memories</p>` +
        data.memories.map(memoryCard).join("");
    }

    async function recall() {
      const data = await api("/v1/memory/recall", {
        method: "POST",
        body: JSON.stringify({
          tenant_id: tenant(), workspace_id: workspace(),
          query: $("query").value || "project memory",
          layers: $("layer").value ? [$("layer").value] : []
        }),
      });
      $("memories").innerHTML = `<pre>${escapeHtml(data.context.markdown || "")}</pre>`;
    }

    async function loadConflicts() {
      const params = new URLSearchParams({ tenant_id: tenant(), include_resolved: "true" });
      const data = await api(`/v1/workspaces/${workspace()}/conflicts?${params}`);
      $("ops").innerHTML = `<p class="muted">${data.count} conflict cases</p>` +
        data.cases.map(c => `<div class="card">
          <div><span class="pill">${c.review_status}</span>${c.subject} / ${c.predicate}</div>
          <div class="muted">suggested: ${escapeHtml(c.suggested_winner_value)}</div>
          ${c.candidates.map(x => `<pre>${x.status}: ${escapeHtml(x.value)}</pre>`).join("")}
        </div>`).join("");
    }

    async function reflect() {
      const data = await api(
        `/v1/workspaces/${workspace()}/reflect?tenant_id=${tenant()}`,
        { method: "POST" },
      );
      $("ops").innerHTML = `<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
    }

    async function reindex() {
      const data = await api(
        `/v1/workspaces/${workspace()}/reindex?tenant_id=${tenant()}`,
        { method: "POST" },
      );
      $("ops").innerHTML = `<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    }

    listMemories().catch(err => $("memories").textContent = err.message);
  </script>
</body>
</html>
"""
