"""FastAPI application factory; imports remain optional for core users."""
# ruff: noqa: E501

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
from typing import Any, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from memory_plane.adapters.documents import BinaryDocumentCommand, DocumentIngestor
from memory_plane.adapters.embeddings import (
    EmbeddingProviderConfig,
    build_embedding_client,
)
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


class ModelSettingsBody(BaseModel):
    """Desired embedding/runtime model settings edited from the operator UI."""

    provider: Literal["fake", "openai", "ollama", "tei"] = "fake"
    model_name: str = Field(min_length=1)
    dimension: int = Field(default=1536, ge=1, le=65536)
    base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = Field(default=30.0, ge=1, le=600)


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


def _settings_path() -> str | None:
    """Return optional JSON path for desired model settings."""
    path = os.getenv("UAM_MODEL_SETTINGS_PATH", "").strip()
    return path or None


def _load_model_settings() -> dict[str, Any] | None:
    """Load desired model settings from disk when configured."""
    path = _settings_path()
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else None


def _save_model_settings(settings: dict[str, Any]) -> None:
    """Persist desired model settings when the server is configured to do so."""
    path = _settings_path()
    if not path:
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _mask_secret(value: str | None) -> str | None:
    """Mask API keys before returning settings to the browser."""
    if not value:
        return None
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}…{value[-4:]}"


def _model_settings_response(
    services: Container,
    desired: dict[str, Any] | None,
) -> dict[str, Any]:
    """Render runtime and desired model settings."""
    client = getattr(services.embedding, "_client", None)
    env_config = EmbeddingProviderConfig.from_env()
    desired_config = desired or _model_body_from_config(env_config)
    safe_desired = {**desired_config, "api_key": _mask_secret(desired_config.get("api_key"))}
    return {
        "runtime": {
            "model_name": getattr(client, "model_name", env_config.model_name),
            "dimension": getattr(client, "dimension", env_config.dimension),
            "provider": env_config.provider,
            "base_url": env_config.base_url,
            "timeout_seconds": env_config.timeout_seconds,
            "qdrant_dimension": getattr(client, "dimension", env_config.dimension),
        },
        "desired": safe_desired,
        "settings_path": _settings_path(),
        "restart_required": True,
        "env": {
            "UAM_EMBEDDING_PROVIDER": desired_config["provider"],
            "UAM_EMBEDDING_MODEL": desired_config["model_name"],
            "UAM_EMBEDDING_DIM": str(desired_config["dimension"]),
            "UAM_EMBEDDING_BASE_URL": desired_config.get("base_url") or "",
            "UAM_EMBEDDING_TIMEOUT_SECONDS": str(desired_config["timeout_seconds"]),
        },
    }


def _model_body_from_config(config: EmbeddingProviderConfig) -> dict[str, Any]:
    """Convert provider config to serializable settings."""
    return {
        "provider": config.provider,
        "model_name": config.model_name,
        "dimension": config.dimension,
        "base_url": config.base_url,
        "api_key": config.api_key,
        "timeout_seconds": config.timeout_seconds,
    }


def _settings_from_body(body: ModelSettingsBody) -> dict[str, Any]:
    """Normalize a model settings payload."""
    return {
        "provider": body.provider,
        "model_name": body.model_name.strip(),
        "dimension": body.dimension,
        "base_url": body.base_url.strip() if body.base_url else None,
        "api_key": body.api_key.strip() if body.api_key else None,
        "timeout_seconds": body.timeout_seconds,
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
    model_settings = _load_model_settings()
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

    @app.get("/v1/settings/models")
    def get_model_settings() -> dict[str, Any]:
        """Return runtime and desired embedding model settings for the UI."""
        return _model_settings_response(services, model_settings)

    @app.put("/v1/settings/models")
    def save_model_settings(body: ModelSettingsBody) -> dict[str, Any]:
        """Save desired model settings without hot-swapping a live vector index."""
        nonlocal model_settings
        previous_settings = model_settings or {}
        model_settings = _settings_from_body(body)
        if model_settings["api_key"] is None:
            existing_key = previous_settings.get("api_key")
            if existing_key:
                model_settings["api_key"] = existing_key
        try:
            _save_model_settings(model_settings)
        except OSError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _model_settings_response(services, model_settings)

    @app.post("/v1/settings/models/test")
    def test_model_settings(body: ModelSettingsBody) -> dict[str, Any]:
        """Probe an embedding provider using the proposed settings."""
        settings = _settings_from_body(body)
        try:
            config = EmbeddingProviderConfig(**settings)
            client = build_embedding_client(config)
            embed_document = getattr(client, "embed_document", None)
            vector = (
                embed_document("universal agent memory embedding healthcheck")
                if callable(embed_document)
                else client.embed("universal agent memory embedding healthcheck")
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        actual = len(vector)
        expected = config.dimension
        return {
            "ok": actual == expected,
            "model_name": client.model_name,
            "provider": config.provider,
            "dimension": actual,
            "expected_dimension": expected,
            "message": (
                "endpoint returned expected vector dimension"
                if actual == expected
                else "endpoint dimension differs from configured Qdrant dimension"
            ),
        }

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
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Универсальная память агентов</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #070914;
      --panel: rgba(17, 24, 39, .74);
      --panel-strong: rgba(15, 23, 42, .94);
      --line: rgba(148, 163, 184, .18);
      --text: #eef4ff;
      --muted: #94a3b8;
      --soft: #cbd5e1;
      --blue: #60a5fa;
      --cyan: #22d3ee;
      --violet: #a78bfa;
      --green: #34d399;
      --amber: #fbbf24;
      --red: #fb7185;
      --shadow: 0 24px 90px rgba(0, 0, 0, .45);
      font-family:
        Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      background:
        radial-gradient(circle at 12% 8%, rgba(96, 165, 250, .28), transparent 32rem),
        radial-gradient(circle at 86% 4%, rgba(167, 139, 250, .26), transparent 30rem),
        radial-gradient(circle at 60% 90%, rgba(34, 211, 238, .16), transparent 34rem),
        linear-gradient(135deg, #060711 0%, #0b1020 48%, #111827 100%);
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(148, 163, 184, .04) 1px, transparent 1px),
        linear-gradient(90deg, rgba(148, 163, 184, .04) 1px, transparent 1px);
      background-size: 42px 42px;
      mask-image: linear-gradient(to bottom, rgba(0, 0, 0, .9), transparent 80%);
    }
    .shell { width: min(1500px, calc(100vw - 36px)); margin: 0 auto; padding: 28px 0; }
    header.hero {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 24px;
      align-items: end;
      margin-bottom: 18px;
      padding: 26px;
      border: 1px solid var(--line);
      border-radius: 28px;
      background: linear-gradient(135deg, rgba(15, 23, 42, .86), rgba(30, 41, 59, .62));
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }
    h1, h2, h3, p { margin-top: 0; }
    h1 { margin-bottom: 8px; font-size: clamp(34px, 5vw, 62px); letter-spacing: -.055em; }
    h2 { margin-bottom: 14px; font-size: 18px; letter-spacing: -.02em; }
    h3 { margin-bottom: 8px; font-size: 14px; color: var(--soft); }
    .lede { max-width: 820px; color: var(--soft); font-size: 16px; line-height: 1.7; }
    .brand {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 14px;
      color: var(--cyan);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .16em;
      text-transform: uppercase;
    }
    .orb {
      width: 12px;
      height: 12px;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--cyan), var(--violet));
      box-shadow: 0 0 24px rgba(34, 211, 238, .75);
    }
    .hero-actions, .row, .tabs, .toolbar { display: flex; gap: 10px; flex-wrap: wrap; }
    .hero-actions { justify-content: flex-end; }
    button, input, select, textarea {
      border: 1px solid var(--line);
      border-radius: 14px;
      color: var(--text);
      background: rgba(15, 23, 42, .76);
      outline: none;
    }
    input, select, textarea { width: 100%; padding: 12px 13px; }
    textarea { min-height: 108px; resize: vertical; }
    input:focus, select:focus, textarea:focus {
      border-color: rgba(96, 165, 250, .78);
      box-shadow: 0 0 0 4px rgba(96, 165, 250, .12);
    }
    button {
      cursor: pointer;
      min-height: 42px;
      padding: 11px 15px;
      font-weight: 750;
      background: linear-gradient(135deg, rgba(96, 165, 250, .98), rgba(167, 139, 250, .92));
      border-color: rgba(147, 197, 253, .45);
      box-shadow: 0 12px 30px rgba(59, 130, 246, .22);
    }
    button:hover { transform: translateY(-1px); }
    button.secondary {
      background: rgba(15, 23, 42, .74);
      color: var(--soft);
      box-shadow: none;
    }
    button.ghost {
      background: transparent;
      color: var(--muted);
      border-color: transparent;
      box-shadow: none;
    }
    .grid {
      display: grid;
      grid-template-columns: 1.25fr .75fr;
      gap: 18px;
      align-items: start;
    }
    .panel {
      border: 1px solid var(--line);
      border-radius: 24px;
      background: var(--panel);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      overflow: hidden;
    }
    .panel-head {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: center;
      padding: 18px 18px 0;
    }
    .panel-body { padding: 18px; }
    .kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .kpi {
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 20px;
      background:
        linear-gradient(135deg, rgba(255,255,255,.08), rgba(255,255,255,.02)),
        rgba(15, 23, 42, .72);
    }
    .kpi .value { font-size: 28px; font-weight: 850; letter-spacing: -.04em; }
    .kpi .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .12em; }
    .cockpit {
      margin: 18px 0;
      background:
        linear-gradient(135deg, rgba(15, 23, 42, .82), rgba(30, 41, 59, .56)),
        radial-gradient(circle at 50% 30%, rgba(96, 165, 250, .13), transparent 28rem);
    }
    .cockpit-layout {
      display: grid;
      grid-template-columns: 230px minmax(0, 1fr) 300px;
      gap: 16px;
      min-height: 440px;
    }
    .side-nav, .inspector {
      border: 1px solid rgba(148, 163, 184, .14);
      border-radius: 20px;
      background: rgba(2, 6, 23, .35);
      padding: 14px;
    }
    .nav-title {
      margin-bottom: 12px;
      color: var(--cyan);
      font-size: 11px;
      font-weight: 850;
      letter-spacing: .15em;
      text-transform: uppercase;
    }
    .nav-button {
      width: 100%;
      justify-content: flex-start;
      margin-bottom: 8px;
      background: rgba(15, 23, 42, .58);
      box-shadow: none;
      color: var(--soft);
    }
    .nav-button.primary {
      background: linear-gradient(135deg, rgba(96, 165, 250, .9), rgba(167, 139, 250, .75));
      color: var(--text);
    }
    .graph-stage {
      position: relative;
      min-height: 420px;
      border: 1px solid rgba(148, 163, 184, .16);
      border-radius: 24px;
      background:
        radial-gradient(circle at 50% 45%, rgba(34, 211, 238, .18), transparent 16rem),
        radial-gradient(circle at 32% 22%, rgba(167, 139, 250, .18), transparent 13rem),
        rgba(2, 6, 23, .46);
      overflow: hidden;
    }
    .graph-stage::before {
      content: "";
      position: absolute;
      inset: 18px;
      border: 1px solid rgba(148, 163, 184, .08);
      border-radius: 999px;
      pointer-events: none;
    }
    .graph-stage::after {
      content: "";
      position: absolute;
      inset: 72px 110px;
      border: 1px dashed rgba(148, 163, 184, .13);
      border-radius: 999px;
      pointer-events: none;
    }
    .stage-head {
      position: absolute;
      z-index: 2;
      top: 18px;
      left: 18px;
      right: 18px;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }
    .stage-title { font-size: 22px; font-weight: 850; letter-spacing: -.035em; }
    #overviewGraph { position: absolute; inset: 0; }
    .overview-svg { position: absolute; inset: 0; width: 100%; height: 100%; }
    .agent-card {
      display: grid;
      gap: 6px;
      padding: 12px;
      border: 1px solid rgba(148, 163, 184, .14);
      border-radius: 16px;
      background: rgba(15, 23, 42, .54);
      margin-bottom: 10px;
    }
    .agent-row {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
    }
    .sparkline {
      height: 6px;
      border-radius: 999px;
      background: linear-gradient(90deg, rgba(34, 211, 238, .85), rgba(167, 139, 250, .85));
      box-shadow: 0 0 22px rgba(96, 165, 250, .25);
    }
    .tabs {
      position: relative;
      padding: 8px;
      border-bottom: 1px solid var(--line);
      background: rgba(2, 6, 23, .35);
    }
    .tab {
      position: relative;
      z-index: 1;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 104px;
      border: 0;
      box-shadow: none;
      background: transparent;
      color: var(--muted);
      min-height: 38px;
    }
    .tab.active {
      color: var(--text);
      background: rgba(96, 165, 250, .18);
      border: 1px solid rgba(96, 165, 250, .24);
    }
    .view { display: none; }
    .view.active { display: block; }
    .form-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .form-grid .wide { grid-column: span 2; }
    .form-grid .full { grid-column: 1 / -1; }
    .card {
      position: relative;
      padding: 15px;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(15, 23, 42, .62);
      overflow: hidden;
    }
    .card + .card { margin-top: 10px; }
    .card::before {
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 3px;
      background: linear-gradient(var(--cyan), var(--violet));
      opacity: .75;
    }
    .memory-text, pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      line-height: 1.55;
    }
    pre {
      margin: 0;
      padding: 14px;
      max-height: 520px;
      overflow: auto;
      border-radius: 16px;
      background: rgba(2, 6, 23, .58);
      border: 1px solid rgba(148, 163, 184, .12);
    }
    .muted { color: var(--muted); }
    .tiny { font-size: 12px; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 9px;
      border-radius: 999px;
      color: var(--soft);
      background: rgba(148, 163, 184, .13);
      border: 1px solid rgba(148, 163, 184, .14);
      font-size: 12px;
      margin: 0 6px 6px 0;
    }
    .pill.ok { color: #bbf7d0; background: rgba(34, 197, 94, .12); }
    .pill.warn { color: #fde68a; background: rgba(245, 158, 11, .12); }
    .pill.hot { color: #fecdd3; background: rgba(244, 63, 94, .12); }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .list { display: grid; gap: 10px; }
    .editor {
      display: grid;
      gap: 10px;
    }
    .editor textarea {
      min-height: 280px;
      font-size: 15px;
      line-height: 1.65;
      background: rgba(2, 6, 23, .44);
    }
    .editor-meta {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      min-height: 34px;
    }
    .log {
      max-height: 360px;
      overflow: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
    }
    .graph-map {
      width: 100%;
      min-height: 360px;
      border-radius: 18px;
      background:
        radial-gradient(circle at 50% 50%, rgba(96, 165, 250, .16), transparent 24rem),
        rgba(2, 6, 23, .45);
      border: 1px solid rgba(148, 163, 184, .14);
      overflow: hidden;
    }
    .graph-map svg { display: block; width: 100%; height: 360px; }
    .graph-node { filter: drop-shadow(0 12px 22px rgba(0, 0, 0, .38)); }
    .graph-label {
      fill: #e5edff;
      font: 700 12px ui-sans-serif, system-ui, sans-serif;
      text-anchor: middle;
    }
    .graph-edge {
      stroke: rgba(148, 163, 184, .55);
      stroke-width: 2;
      marker-end: url(#arrow);
    }
    .graph-edge-hot { stroke: rgba(251, 113, 133, .86); }
    .graph-edge-ok { stroke: rgba(52, 211, 153, .78); }
    .graph-edge-warn { stroke: rgba(251, 191, 36, .78); }
    .force-graph {
      position: relative;
      width: 100%;
      min-height: 520px;
      border-radius: 22px;
      border: 1px solid rgba(148, 163, 184, .16);
      background:
        radial-gradient(circle at 50% 50%, rgba(34, 211, 238, .14), transparent 18rem),
        radial-gradient(circle at 22% 16%, rgba(167, 139, 250, .18), transparent 16rem),
        rgba(2, 6, 23, .52);
      overflow: hidden;
      touch-action: none;
    }
    .force-graph::before {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(148, 163, 184, .045) 1px, transparent 1px),
        linear-gradient(90deg, rgba(148, 163, 184, .045) 1px, transparent 1px);
      background-size: 34px 34px;
      mask-image: radial-gradient(circle at 50% 50%, #000, transparent 78%);
    }
    .force-graph.compact { position: absolute; inset: 0; min-height: 420px; height: 100%; border: 0; background: transparent; }
    .force-svg {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      cursor: grab;
    }
    .force-svg.dragging { cursor: grabbing; }
    .node-glow { filter: drop-shadow(0 16px 26px rgba(0, 0, 0, .48)); }
    .node-ring { fill: rgba(15, 23, 42, .86); stroke: rgba(226, 232, 240, .18); stroke-width: 1.5; }
    .node-core { stroke: rgba(255, 255, 255, .38); stroke-width: 1.2; }
    .node-label {
      fill: #f8fbff;
      font: 800 12px ui-sans-serif, system-ui, sans-serif;
      text-anchor: middle;
      paint-order: stroke;
      stroke: rgba(2, 6, 23, .86);
      stroke-width: 4px;
      stroke-linejoin: round;
      pointer-events: none;
    }
    .edge-line { stroke: rgba(148, 163, 184, .45); stroke-width: 1.8; }
    .edge-line.hot { stroke: rgba(251, 113, 133, .84); }
    .edge-line.ok { stroke: rgba(52, 211, 153, .76); }
    .edge-line.warn { stroke: rgba(251, 191, 36, .78); }
    .graph-tools {
      position: absolute;
      z-index: 4;
      right: 14px;
      bottom: 14px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      max-width: min(620px, calc(100% - 28px));
      padding: 10px;
      border: 1px solid rgba(148, 163, 184, .16);
      border-radius: 18px;
      background: rgba(2, 6, 23, .72);
      backdrop-filter: blur(14px);
    }
    .graph-tools label {
      display: inline-flex;
      gap: 8px;
      align-items: center;
      color: var(--soft);
      font-size: 12px;
    }
    .graph-tools input[type="range"] { width: 110px; padding: 0; accent-color: var(--cyan); }
    .graph-tools input[type="checkbox"] { width: auto; accent-color: var(--cyan); }
    .dashboard-tiles {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-top: 12px;
    }
    .status-tile {
      padding: 14px;
      border: 1px solid rgba(148, 163, 184, .14);
      border-radius: 18px;
      background: linear-gradient(135deg, rgba(255,255,255,.075), rgba(255,255,255,.018));
    }
    .settings-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .settings-grid .full { grid-column: 1 / -1; }
    .env-block {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      line-height: 1.6;
      color: #dbeafe;
    }
    .legend {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 10px;
    }
    .empty {
      padding: 28px;
      border: 1px dashed rgba(148, 163, 184, .25);
      border-radius: 18px;
      color: var(--muted);
      text-align: center;
    }
    a { color: var(--cyan); }
    @media (max-width: 1100px) {
      header.hero, .grid, .split, .cockpit-layout { grid-template-columns: 1fr; }
      .hero-actions { justify-content: flex-start; }
      .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .form-grid { grid-template-columns: 1fr 1fr; }
    }
    @media (max-width: 680px) {
      .shell { width: min(100vw - 20px, 1500px); padding: 10px 0; }
      header.hero { border-radius: 20px; padding: 18px; }
      .kpis, .form-grid { grid-template-columns: 1fr; }
      .form-grid .wide { grid-column: auto; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="hero">
      <div>
        <div class="brand"><span class="orb"></span> Локальный пульт памяти</div>
        <h1>Универсальная память агентов</h1>
        <p class="lede">
          Современная консоль для общей долговременной памяти: поиск и recall,
          разбор конфликтов, экспорт Obsidian‑хранилища и подробный граф связей
          вокруг каждого воспоминания. Все записи идут через append-only API.
        </p>
      </div>
      <div class="hero-actions">
        <button onclick="refreshAll()">Обновить пульт</button>
        <button class="secondary" onclick="showTab('vault')">Открыть хранилище</button>
      </div>
    </header>

    <section class="panel">
      <div class="panel-body">
        <div class="form-grid">
          <label class="wide">
            <span class="muted tiny">Арендатор</span>
            <input id="tenant" placeholder="tenant_id"
              value="00000000-0000-0000-0000-000000000001">
          </label>
          <label class="wide">
            <span class="muted tiny">Рабочая область</span>
            <input id="workspace" placeholder="workspace_id"
              value="00000000-0000-0000-0000-000000000002">
          </label>
        </div>
        <div class="kpis">
          <div class="kpi"><div id="kpiMemories" class="value">—</div><div class="label">Воспоминания</div></div>
          <div class="kpi"><div id="kpiConflicts" class="value">—</div><div class="label">Конфликты</div></div>
          <div class="kpi"><div id="kpiVault" class="value">—</div><div class="label">Файлы хранилища</div></div>
          <div class="kpi"><div id="kpiStatus" class="value">Активно</div><div class="label">Статус</div></div>
        </div>
      </div>
    </section>

    <section class="panel cockpit" aria-label="Главная карта памяти">
      <div class="panel-body cockpit-layout">
        <nav class="side-nav" role="navigation" aria-label="Быстрая навигация пульта">
          <div class="nav-title">Навигация</div>
          <button class="nav-button primary" onclick="showTab('memory')">🧠 Память и recall</button>
          <button class="nav-button" onclick="showTab('graph')">🕸️ Карта связей</button>
          <button class="nav-button" onclick="showTab('conflicts')">⚠️ Конфликты</button>
          <button class="nav-button" onclick="showTab('vault')">🗂️ Obsidian vault</button>
          <button class="nav-button" onclick="showTab('settings')">⚙️ Модели</button>
          <button class="nav-button" onclick="showTab('retain')">✍️ Новая память</button>
          <div class="nav-title" style="margin-top:18px">Слои</div>
          <span class="pill">ядро</span><span class="pill">рабочая</span>
          <span class="pill ok">семантика</span><span class="pill warn">спорное</span>
        </nav>

        <div class="graph-stage">
          <div class="stage-head">
            <div>
              <div class="brand" style="margin-bottom:6px"><span class="orb"></span> Живая карта памяти</div>
              <div class="stage-title">Общая память как граф</div>
              <p class="muted tiny">Центр — рабочая область; вокруг — свежие воспоминания, конфликтные зоны и агентские подключения.</p>
            </div>
            <button class="secondary" onclick="refreshAll()">Синхронизировать</button>
          </div>
          <div id="overviewGraph"></div>
          <div class="dashboard-tiles" style="position:absolute;left:18px;right:18px;bottom:18px;z-index:3">
            <div class="status-tile"><div class="muted tiny">Индексация</div><strong id="dashIndex">готово</strong></div>
            <div class="status-tile"><div class="muted tiny">Embedding</div><strong id="dashModel">загрузка…</strong></div>
            <div class="status-tile"><div class="muted tiny">Граф</div><strong>drag / zoom</strong></div>
          </div>
        </div>

        <aside class="inspector" aria-label="Инспектор системы памяти">
          <div class="nav-title">Инспектор</div>
          <div class="agent-card">
            <div class="agent-row"><strong>OpenClaw</strong><span class="pill ok">plugin-ready</span></div>
            <div class="muted tiny">Recall перед запуском, retain после tool loop.</div>
            <div class="sparkline" style="width:82%"></div>
          </div>
          <div class="agent-card">
            <div class="agent-row"><strong>Hermes</strong><span class="pill ok">plugin-ready</span></div>
            <div class="muted tiny">Prefetch перед turn, sync после ответа.</div>
            <div class="sparkline" style="width:74%"></div>
          </div>
          <div id="selectionInspector" class="agent-card">
            <div class="agent-row"><strong>Выбор</strong><span class="pill">workspace</span></div>
            <div class="muted tiny">Выбери воспоминание или узел графа, чтобы увидеть детали.</div>
          </div>
        </aside>
      </div>
    </section>

    <main class="grid">
      <section class="panel">
        <div class="tabs" role="tablist" aria-label="Разделы пульта памяти">
          <button id="tab-memory" type="button" class="tab active" role="tab" aria-controls="view-memory" onclick="showTab('memory')">Память</button>
          <button id="tab-retain" type="button" class="tab" role="tab" aria-controls="view-retain" onclick="showTab('retain')">Записать</button>
          <button id="tab-conflicts" type="button" class="tab" role="tab" aria-controls="view-conflicts" onclick="showTab('conflicts')">Конфликты</button>
          <button id="tab-vault" type="button" class="tab" role="tab" aria-controls="view-vault" onclick="showTab('vault')">Хранилище</button>
          <button id="tab-graph" type="button" class="tab" role="tab" aria-controls="view-graph" onclick="showTab('graph')">Граф</button>
          <button id="tab-settings" type="button" class="tab" role="tab" aria-controls="view-settings" onclick="showTab('settings')">Модели</button>
        </div>

        <div id="view-memory" class="view active">
          <div class="panel-head">
            <div>
              <h2>Поиск и recall</h2>
              <p class="muted tiny">Список воспоминаний или сборка контекстного пакета для агента.</p>
            </div>
          </div>
          <div class="panel-body">
            <div class="form-grid">
              <input id="query" class="wide" aria-label="Запрос для семантического поиска" placeholder="запрос для семантического поиска">
              <select id="layer" aria-label="Фильтр слоя памяти">
                <option value="">все слои</option>
                <option>core</option><option>working</option><option>semantic</option>
                <option>episodic</option><option>procedural</option><option>social</option>
                <option>reflection</option><option>error</option>
              </select>
              <select id="status" aria-label="Фильтр статуса памяти">
                <option value="">все статусы</option>
                <option>active</option><option>stale</option><option>disputed</option>
                <option>rejected</option><option>archived</option><option>pinned</option>
              </select>
              <input id="label" aria-label="Фильтр по метке" placeholder="фильтр по метке">
              <button onclick="listMemories()">Показать память</button>
              <button class="secondary" onclick="recall()">Собрать recall</button>
            </div>
            <div id="memories" class="list"></div>
          </div>
        </div>

        <div id="view-retain" class="view">
          <div class="panel-head">
            <div>
              <h2>Записать воспоминание</h2>
              <p class="muted tiny">Создаёт обычную запись через `/v1/memory/retain`.</p>
            </div>
          </div>
          <div class="panel-body">
            <div class="form-grid">
              <select id="retainLayer" aria-label="Слой новой памяти"><option>semantic</option><option>core</option><option>working</option><option>episodic</option><option>procedural</option><option>social</option><option>reflection</option><option>error</option></select>
              <select id="retainScope" aria-label="Область новой памяти"><option>workspace</option><option>thread</option><option>agent</option><option>tenant</option></select>
              <input id="retainKind" aria-label="Тип новой записи" placeholder="тип записи" value="operator_note">
              <input id="retainLabels" aria-label="Метки новой записи" placeholder="метки через запятую" value="ui">
              <textarea id="retainText" class="full" aria-label="Текст нового воспоминания" placeholder="Запиши устойчивый факт, решение, предпочтение или наблюдение..."></textarea>
              <button onclick="retainMemory()">Сохранить память</button>
              <button class="secondary" onclick="$('retainText').value=''">Очистить</button>
            </div>
            <div id="retainResult"></div>
          </div>
        </div>

        <div id="view-conflicts" class="view">
          <div class="panel-head">
            <div>
              <h2>Входящие конфликты</h2>
              <p class="muted tiny">Разбирай пересекающиеся факты, не уничтожая историю доказательств.</p>
            </div>
            <button class="secondary" onclick="loadConflicts()">Обновить</button>
          </div>
          <div class="panel-body"><div id="ops" class="list"></div></div>
        </div>

        <div id="view-vault" class="view">
          <div class="panel-head">
            <div>
              <h2>Obsidian‑хранилище</h2>
              <p class="muted tiny">Редактируй обычный текст памяти. Frontmatter, ревизии и embedding остаются под капотом.</p>
            </div>
            <div class="toolbar">
              <button class="secondary" onclick="loadVault()">Обновить хранилище</button>
              <button class="secondary" onclick="planEditedVault()">Проверить изменения</button>
              <button onclick="saveEditedVault()">Сохранить и пересчитать embedding</button>
            </div>
          </div>
          <div class="panel-body split">
            <div id="vaultFiles" class="list"></div>
            <div class="editor">
              <div id="vaultMeta" class="editor-meta muted tiny">Выбери воспоминание…</div>
              <textarea id="vaultEditor" aria-label="Редактор текста воспоминания" placeholder="Текст выбранного воспоминания…"></textarea>
              <div class="toolbar">
                <button class="secondary" onclick="copyVaultText()">Копировать текст</button>
                <button class="secondary" onclick="resetVaultEditor()">Сбросить</button>
              </div>
              <div id="vaultResult"></div>
            </div>
          </div>
        </div>

        <div id="view-graph" class="view">
          <div class="panel-head">
            <div>
              <h2>Подробный граф памяти</h2>
              <p class="muted tiny">Obsidian‑style карта: узлы можно тянуть, колесом масштабировать, фон перетаскивать.</p>
            </div>
          </div>
          <div class="panel-body">
            <div class="form-grid">
              <input id="graphItem" class="wide" aria-label="ID воспоминания для графа" placeholder="id воспоминания">
              <select id="edgeType" aria-label="Фильтр типа связи графа">
                <option value="">все типы связей</option>
                <option>supports</option><option>contradicts</option><option>supersedes</option>
                <option>derived_from</option><option>related_to</option><option>blocks</option>
                <option>resolves</option>
              </select>
              <button onclick="loadGraph()">Показать связи</button>
            </div>
            <div id="graphCanvas" class="card"></div>
            <div id="graph" class="list"></div>
          </div>
        </div>

        <div id="view-settings" class="view">
          <div class="panel-head">
            <div>
              <h2>Настройки моделей</h2>
              <p class="muted tiny">Настраивай embedding provider прямо из web. Применение к Qdrant — через restart + reindex, чтобы не смешивать размерности.</p>
            </div>
            <button class="secondary" onclick="loadModelSettings()">Обновить</button>
          </div>
          <div class="panel-body">
            <div class="settings-grid">
              <label>
                <span class="muted tiny">Provider</span>
                <select id="modelProvider" aria-label="Embedding provider">
                  <option value="fake">fake / тестовый</option>
                  <option value="tei">TEI / llama.cpp OpenAI-compatible</option>
                  <option value="ollama">Ollama</option>
                  <option value="openai">OpenAI</option>
                </select>
              </label>
              <label>
                <span class="muted tiny">Model</span>
                <input id="modelName" aria-label="Embedding model" placeholder="jina-embeddings-v4">
              </label>
              <label>
                <span class="muted tiny">Dimension</span>
                <input id="modelDim" aria-label="Embedding dimension" type="number" min="1" max="65536" value="1536">
              </label>
              <label>
                <span class="muted tiny">Timeout seconds</span>
                <input id="modelTimeout" aria-label="Embedding timeout" type="number" min="1" max="600" value="30">
              </label>
              <label class="full">
                <span class="muted tiny">Base URL</span>
                <input id="modelBaseUrl" aria-label="Embedding base URL" placeholder="http://192.168.0.10:8081">
              </label>
              <label class="full">
                <span class="muted tiny">API key, если нужен</span>
                <input id="modelApiKey" aria-label="Embedding API key" placeholder="оставь пустым, если endpoint локальный">
              </label>
            </div>
            <div class="toolbar" style="margin-top:12px">
              <button onclick="saveModelSettings()">Сохранить desired config</button>
              <button class="secondary" onclick="testModelSettings()">Проверить endpoint</button>
              <button class="secondary" onclick="reindex()">Reindex после применения</button>
            </div>
            <div class="split" style="margin-top:12px">
              <div class="card">
                <h3>Runtime сейчас</h3>
                <div id="runtimeSettings" class="muted tiny">Загрузка…</div>
              </div>
              <div class="card">
                <h3>Docker env для применения</h3>
                <pre id="modelEnv" class="env-block">Загрузка…</pre>
              </div>
            </div>
            <div id="modelSettingsResult" style="margin-top:12px"></div>
          </div>
        </div>
      </section>

      <aside class="panel">
        <div class="panel-head">
          <div>
            <h2>Операции</h2>
            <p class="muted tiny">Безопасные действия сервера и живой журнал команд.</p>
          </div>
        </div>
        <div class="panel-body">
          <div class="toolbar">
            <button onclick="reflect()">Рефлексия</button>
            <button class="secondary" onclick="reindex()">Переиндексация</button>
            <button class="secondary" onclick="loadConflicts()">Входящие</button>
          </div>
          <h3 style="margin-top:18px">Журнал действий</h3>
          <div id="log" class="log"><div class="muted">Готово.</div></div>
        </div>
      </aside>
    </main>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    const tenant = () => $("tenant").value.trim();
    const workspace = () => $("workspace").value.trim();
    let lastMemories = [];
    const graphInstances = {};

    async function api(path, options = {}) {
      log(`→ ${options.method || "GET"} ${path}`);
      const res = await fetch(path, {
        ...options,
        headers: { "content-type": "application/json", ...(options.headers || {}) },
      });
      const text = await res.text();
      let data = {};
      try {
        data = text ? JSON.parse(text) : {};
      } catch {
        data = { detail: text || "Ответ сервера не похож на JSON." };
      }
      if (!res.ok) {
        log(`× ${res.status} ${path}`);
        throw new Error(JSON.stringify(data));
      }
      log(`✓ ${res.status} ${path}`);
      return data;
    }

    function showTab(name) {
      document.querySelectorAll(".tab").forEach(node => {
        node.classList.remove("active");
        node.setAttribute("aria-selected", "false");
      });
      document.querySelectorAll(".view").forEach(node => node.classList.remove("active"));
      $(`tab-${name}`).classList.add("active");
      $(`tab-${name}`).setAttribute("aria-selected", "true");
      $(`view-${name}`).classList.add("active");
      if (name === "conflicts") loadConflicts();
      if (name === "vault") loadVault();
      if (name === "graph") loadGraph();
      if (name === "settings") loadModelSettings();
    }

    async function refreshAll() {
      await Promise.allSettled([listMemories(), loadConflicts(), loadVault(), loadModelSettings()]);
    }

    function updateKpis({ memories, conflicts, vault } = {}) {
      if (memories != null) $("kpiMemories").textContent = memories;
      if (conflicts != null) $("kpiConflicts").textContent = conflicts;
      if (vault != null) $("kpiVault").textContent = vault;
      $("kpiStatus").textContent = "Активно";
    }

    function memoryCard(row) {
      const statusClass = row.status === "active" || row.status === "pinned" ? "ok"
        : row.status === "disputed" || row.status === "stale" ? "warn" : "hot";
      return `<div class="card">
        <div>
          <span class="pill">${escapeHtml(layerName(row.layer))}</span>
          <span class="pill ${statusClass}">${escapeHtml(statusName(row.status))}</span>
          <span class="pill">${escapeHtml(row.kind)}</span>
          <span class="muted tiny">ревизия ${row.revision} · уверенность ${Number(row.confidence).toFixed(2)}</span>
        </div>
        <div class="memory-text">${escapeHtml(row.text)}</div>
        <div class="row" style="margin-top:12px">
          <button class="secondary" onclick="inspectGraph('${row.id}')">Граф</button>
          <button class="ghost" onclick="copyText('${row.id}')">Скопировать id</button>
        </div>
        <div class="muted tiny">${escapeHtml(row.id)} · ${escapeHtml(row.created_at || "")}</div>
      </div>`;
    }

    async function listMemories() {
      const params = new URLSearchParams({ tenant_id: tenant() });
      if ($("layer").value) params.set("layer", $("layer").value);
      if ($("status").value) params.set("status", $("status").value);
      if ($("label").value) params.set("label", $("label").value);
      const data = await api(`/v1/workspaces/${workspace()}/memories?${params}`);
      lastMemories = data.memories || [];
      updateKpis({ memories: data.count });
      renderOverview();
      $("memories").innerHTML = data.count
        ? data.memories.map(memoryCard).join("")
        : `<div class="empty">Под текущие фильтры воспоминаний нет.</div>`;
    }

    async function recall() {
      const data = await api("/v1/memory/recall", {
        method: "POST",
        body: JSON.stringify({
          tenant_id: tenant(), workspace_id: workspace(),
          query: $("query").value || "память проекта",
          layers: $("layer").value ? [$("layer").value] : []
        }),
      });
      $("memories").innerHTML = `<div class="card">
        <div><span class="pill ok">контекст recall</span>
        <span class="muted tiny">${(data.sources_used || []).join(", ") || "источников нет"}</span></div>
        <pre>${escapeHtml(data.context.markdown || "Подходящая память не найдена.")}</pre>
      </div>`;
    }

    async function retainMemory() {
      const text = $("retainText").value.trim();
      if (!text) {
        $("retainResult").innerHTML = `<div class="empty">Сначала напиши текст воспоминания.</div>`;
        return;
      }
      const payload = {
        tenant_id: tenant(),
        workspace_id: workspace(),
        layer: $("retainLayer").value,
        scope: $("retainScope").value,
        kind: $("retainKind").value || "operator_note",
        text,
        source_kind: "operator-ui",
        labels: $("retainLabels").value.split(",").map(x => x.trim()).filter(Boolean),
      };
      const data = await api("/v1/memory/retain", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      $("retainResult").innerHTML = `<div class="card">
        <span class="pill ok">${data.created ? "создано" : "дубликат"}</span>
        <span class="muted tiny">${escapeHtml(data.id)} · ревизия ${data.revision}</span>
      </div>`;
      await listMemories();
    }

    function readModelSettingsForm() {
      return {
        provider: $("modelProvider").value,
        model_name: $("modelName").value.trim() || "fake-embed-v1",
        dimension: Number($("modelDim").value || 1536),
        base_url: $("modelBaseUrl").value.trim() || null,
        api_key: $("modelApiKey").value.trim() || null,
        timeout_seconds: Number($("modelTimeout").value || 30),
      };
    }

    function applyModelSettings(data) {
      const desired = data.desired || {};
      $("modelProvider").value = desired.provider || "fake";
      $("modelName").value = desired.model_name || "fake-embed-v1";
      $("modelDim").value = desired.dimension || 1536;
      $("modelBaseUrl").value = desired.base_url || "";
      $("modelTimeout").value = desired.timeout_seconds || 30;
      $("modelApiKey").value = "";
      const runtime = data.runtime || {};
      $("dashModel").textContent = `${runtime.provider || "?"} · ${runtime.model_name || "?"}`;
      $("runtimeSettings").innerHTML = `
        <div><span class="pill ok">${escapeHtml(runtime.provider || "unknown")}</span>
        <span class="pill">${escapeHtml(runtime.model_name || "unknown")}</span>
        <span class="pill">${escapeHtml(runtime.dimension || "—")} dim</span></div>
        <div>base: ${escapeHtml(runtime.base_url || "local/default")}</div>
        <div>timeout: ${escapeHtml(runtime.timeout_seconds || "—")}s</div>
        <div class="muted tiny">restart_required: ${data.restart_required ? "да" : "нет"} · settings_path: ${escapeHtml(data.settings_path || "in-memory only")}</div>
      `;
      const env = data.env || {};
      $("modelEnv").textContent = Object.entries(env)
        .map(([key, value]) => `${key}=${value}`)
        .join("\\n") || "env пока не сформирован";
    }

    async function loadModelSettings() {
      const data = await api("/v1/settings/models");
      applyModelSettings(data);
      $("modelSettingsResult").innerHTML = "";
    }

    async function saveModelSettings() {
      const data = await api("/v1/settings/models", {
        method: "PUT",
        body: JSON.stringify(readModelSettingsForm()),
      });
      applyModelSettings(data);
      $("modelSettingsResult").innerHTML = `<div class="card">
        <span class="pill ok">desired config сохранён</span>
        <div class="muted tiny">Чтобы реально применить модель к Qdrant: обнови env Docker, перезапусти сервер/worker и запусти reindex.</div>
      </div>`;
    }

    async function testModelSettings() {
      try {
        const data = await api("/v1/settings/models/test", {
          method: "POST",
          body: JSON.stringify(readModelSettingsForm()),
        });
        $("modelSettingsResult").innerHTML = `<div class="card">
          <span class="pill ${data.ok ? "ok" : "warn"}">${data.ok ? "endpoint работает" : "dimension mismatch"}</span>
          <div class="muted tiny">${escapeHtml(data.provider)} · ${escapeHtml(data.model_name)} · ${escapeHtml(data.dimension)} / ${escapeHtml(data.expected_dimension)} dim</div>
          <div>${escapeHtml(data.message)}</div>
        </div>`;
      } catch (err) {
        $("modelSettingsResult").innerHTML = `<div class="empty">Проверка endpoint не прошла: ${escapeHtml(err.message)}</div>`;
      }
    }

    async function loadConflicts() {
      const params = new URLSearchParams({ tenant_id: tenant(), include_resolved: "true" });
      const data = await api(`/v1/workspaces/${workspace()}/conflicts?${params}`);
      updateKpis({ conflicts: data.count });
      $("ops").innerHTML = data.count ? data.cases.map(c => `<div class="card">
          <div>
            <span class="pill ${c.review_status === "open" ? "warn" : "ok"}">${escapeHtml(reviewName(c.review_status))}</span>
            <strong>${escapeHtml(c.subject)} / ${escapeHtml(c.predicate)}</strong>
          </div>
          <p class="muted tiny">${escapeHtml(reasonName(c.suggested_reason))}</p>
          <div class="pill ok">рекомендация: ${escapeHtml(c.suggested_winner_value || "—")}</div>
          ${c.candidates.map(x => `<pre>${escapeHtml(statusName(x.status))} · уверенность ${Number(x.confidence).toFixed(2)}\\n${escapeHtml(x.value)}</pre>`).join("")}
        </div>`).join("") : `<div class="empty">Конфликтов нет. Память спокойна — подозрительно спокойна.</div>`;
    }

    async function loadVault() {
      const params = new URLSearchParams({ tenant_id: tenant() });
      const data = await api(`/v1/workspaces/${workspace()}/vault?${params}`);
      updateKpis({ vault: data.file_count });
      const files = data.files || [];
      const editable = files
        .map((file, index) => ({ file, index, note: parseVaultNote(file.content) }))
        .filter(row => row.note.frontmatter.type === "memory");
      $("vaultFiles").innerHTML = editable.length ? editable.map(({ file, index, note }) => `
        <button class="secondary" style="width:100%;justify-content:flex-start"
          onclick="previewVault(${index})">
          ${escapeHtml(file.path)}
          <span class="pill ${note.frontmatter.status === "superseded" ? "warn" : "ok"}" style="margin-left:auto">${escapeHtml(statusName(note.frontmatter.status))}</span>
        </button>
      `).join("") : `<div class="empty">Редактируемых воспоминаний нет. README/reflections скрыты из редактора.</div>`;
      window.__vaultFiles = files;
      window.__vaultEditable = editable.map(row => row.index);
      window.__vaultSelected = editable[0]?.index ?? -1;
      if (window.__vaultSelected >= 0) {
        previewVault(window.__vaultSelected);
      } else {
        $("vaultMeta").textContent = "Нет memory-файлов для редактирования.";
        $("vaultEditor").value = "";
      }
    }

    function previewVault(index) {
      const file = (window.__vaultFiles || [])[index];
      window.__vaultSelected = index;
      if (!file) {
        $("vaultMeta").textContent = "Файл не найден.";
        $("vaultEditor").value = "";
        return;
      }
      const note = parseVaultNote(file.content);
      $("vaultEditor").value = note.body;
      $("vaultMeta").innerHTML = `
        <span class="pill">${escapeHtml(file.path)}</span>
        <span class="pill">${escapeHtml(note.frontmatter.id || "без id")}</span>
        <span class="pill">ревизия ${escapeHtml(note.frontmatter.revision || "—")}</span>
        <span class="pill ${note.frontmatter.status === "superseded" ? "warn" : "ok"}">${escapeHtml(statusName(note.frontmatter.status))}</span>
      `;
      $("vaultResult").innerHTML = "";
    }

    async function copyVaultText() {
      const files = window.__vaultFiles || [];
      const selected = files[window.__vaultSelected || 0];
      if (!selected) {
        log("нечего копировать: воспоминание не выбрано");
        return;
      }
      await navigator.clipboard.writeText($("vaultEditor").value);
      log(`скопирован текст ${selected.path}`);
    }

    function resetVaultEditor() {
      if ((window.__vaultSelected ?? -1) >= 0) previewVault(window.__vaultSelected);
    }

    async function planEditedVault() {
      return importEditedVault(true);
    }

    async function saveEditedVault() {
      const result = await importEditedVault(false);
      if (!result) return;
      await reindex();
      await Promise.allSettled([loadVault(), listMemories()]);
    }

    async function importEditedVault(dryRun) {
      const files = window.__vaultFiles || [];
      const selected = files[window.__vaultSelected ?? -1];
      if (!selected) {
        $("vaultResult").innerHTML = `<div class="empty">Сначала выбери воспоминание.</div>`;
        return null;
      }
      const note = parseVaultNote(selected.content);
      if (note.frontmatter.type !== "memory") {
        $("vaultResult").innerHTML = `<div class="empty">Этот файл служебный и не редактируется через web UI.</div>`;
        return null;
      }
      const content = composeVaultNote(note, $("vaultEditor").value);
      const data = await api(`/v1/workspaces/${workspace()}/vault/import`, {
        method: "POST",
        body: JSON.stringify({
          tenant_id: tenant(),
          dry_run: dryRun,
          files: [{ path: selected.path, content }],
        }),
      });
      const change = data.changes?.[0] || {};
      const action = actionName(change.action);
      $("vaultResult").innerHTML = `<div class="card">
        <span class="pill ${change.action === "supersede" ? "ok" : "warn"}">${escapeHtml(action)}</span>
        <span class="muted tiny">${escapeHtml(change.message || "")}</span>
        ${change.new_item_id ? `<div class="muted tiny">новая ревизия: ${escapeHtml(change.new_item_id)}</div>` : ""}
        <div class="muted tiny">${dryRun ? "Проверка без записи." : "Сохранено через append-only supersede; embedding пересчитан через reindex."}</div>
      </div>`;
      return data;
    }

    function inspectGraph(id) {
      $("graphItem").value = id;
      showTab("graph");
      loadGraph();
    }

    async function loadGraph() {
      const item = $("graphItem").value.trim();
      if (!item) {
        $("graphCanvas").innerHTML = renderGraphHost("detail", "Выбери воспоминание, чтобы увидеть карту связей");
        mountForceGraph("detailGraphHost", [{ id: "workspace", label: "workspace", role: "center" }], [], { compact: false });
        $("graph").innerHTML = `<div class="empty">Сначала вставь или выбери id воспоминания.</div>`;
        return;
      }
      const params = new URLSearchParams({ tenant_id: tenant(), workspace_id: workspace() });
      if ($("edgeType").value) params.set("edge_type", $("edgeType").value);
      try {
        const data = await api(`/v1/memory/${item}/neighbors?${params}`);
        $("graphCanvas").innerHTML = renderGraphHost("detail", "Граф связей воспоминания");
        mountForceGraph("detailGraphHost", graphNodesFromEdges(data.edges || [], item), data.edges || [], { compact: false });
        $("graph").innerHTML = data.count ? data.edges.map(edge => `<div class="card">
          <span class="pill">${escapeHtml(edgeName(edge.edge_type))}</span>
          <span class="pill">вес ${Number(edge.weight).toFixed(2)}</span>
          <pre>${escapeHtml(edge.src_id)}\\n→ ${escapeHtml(edge.dst_id)}</pre>
        </div>`).join("") : `<div class="empty">У этого воспоминания пока нет связей графа.</div>`;
      } catch (err) {
        $("graphCanvas").innerHTML = renderGraphHost("detail", "Не удалось загрузить граф");
        mountForceGraph("detailGraphHost", [{ id: item, label: shortId(item), role: "center" }], [], { compact: false });
        $("graph").innerHTML = `<div class="empty">Не удалось загрузить граф: ${escapeHtml(err.message)}</div>`;
      }
    }

    async function reflect() {
      const data = await api(
        `/v1/workspaces/${workspace()}/reflect?tenant_id=${tenant()}`,
        { method: "POST" },
      );
      log(JSON.stringify(data));
      await Promise.allSettled([listMemories(), loadConflicts()]);
    }

    async function reindex() {
      const data = await api(
        `/v1/workspaces/${workspace()}/reindex?tenant_id=${tenant()}`,
        { method: "POST" },
      );
      log(JSON.stringify(data));
    }

    async function copyText(value) {
      await navigator.clipboard.writeText(value);
      log(`скопировано ${value}`);
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    }

    function parseVaultNote(content) {
      const lines = String(content || "").split(/\\r?\\n/);
      if (lines[0] !== "---") return { frontmatter: {}, frontmatterBlock: "", body: content || "", tail: "" };
      const end = lines.findIndex((line, index) => index > 0 && line === "---");
      if (end < 0) return { frontmatter: {}, frontmatterBlock: "", body: content || "", tail: "" };
      const frontmatterLines = lines.slice(1, end);
      const frontmatter = parseFrontmatter(frontmatterLines);
      const bodyLines = lines.slice(end + 1);
      const sectionIndex = bodyLines.findIndex(line =>
        ["## Provenance", "## Quote", "## Links", "## Evidence"].includes(line)
      );
      const editableBody = (sectionIndex >= 0 ? bodyLines.slice(0, sectionIndex) : bodyLines).join("\\n").trim();
      const tail = sectionIndex >= 0 ? "\\n\\n" + bodyLines.slice(sectionIndex).join("\\n").trim() : "";
      return {
        frontmatter,
        frontmatterBlock: lines.slice(0, end + 1).join("\\n"),
        body: editableBody,
        tail,
      };
    }

    function parseFrontmatter(lines) {
      const result = {};
      let currentKey = null;
      for (const line of lines) {
        if (line.startsWith("  - ") && currentKey) {
          result[currentKey] = result[currentKey] || [];
          result[currentKey].push(parseYamlScalar(line.slice(4).trim()));
          continue;
        }
        const match = line.match(/^([^:]+):\\s*(.*)$/);
        if (!match) continue;
        currentKey = match[1];
        const raw = match[2];
        result[currentKey] = raw === "" ? [] : parseYamlScalar(raw);
      }
      return result;
    }

    function parseYamlScalar(raw) {
      if (raw === "null") return null;
      if (raw === "true") return true;
      if (raw === "false") return false;
      if (raw === "[]") return [];
      if (raw === "{}") return {};
      if (raw.startsWith('"') && raw.endsWith('"')) return raw.slice(1, -1).replace(/\\\\"/g, '"').replace(/\\\\\\\\/g, "\\\\");
      const number = Number(raw);
      return Number.isFinite(number) && raw.trim() !== "" ? number : raw;
    }

    function composeVaultNote(note, body) {
      const cleanBody = String(body || "").trim();
      return `${note.frontmatterBlock}\\n\\n${cleanBody}${note.tail ? note.tail : ""}\\n`;
    }

    function log(message) {
      const line = document.createElement("div");
      line.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
      $("log").prepend(line);
    }

    function renderGraphHost(prefix, title) {
      return `<div id="${prefix}GraphHost" class="force-graph ${prefix === "overview" ? "compact" : ""}" aria-label="${escapeHtml(title)}">
        <svg class="force-svg" role="img"></svg>
        <div class="graph-tools">
          <button class="secondary" onclick="restartGraph('${prefix}GraphHost')">Перемешать</button>
          <button class="secondary" onclick="fitGraph('${prefix}GraphHost')">В центр</button>
          <label>сила <input id="${prefix}Gravity" type="range" min="0.2" max="2.2" step="0.1" value="1"></label>
          <label><input id="${prefix}Labels" type="checkbox" checked onchange="toggleGraphLabels('${prefix}GraphHost', this.checked)"> подписи</label>
        </div>
      </div>`;
    }

    function graphNodesFromEdges(edges, center = "") {
      const nodes = new Map();
      const byMemory = new Map((lastMemories || []).map(row => [row.id, row]));
      if (center) {
        const memory = byMemory.get(center);
        nodes.set(center, {
          id: center,
          label: memory ? layerName(memory.layer) : shortId(center),
          text: memory?.text || "",
          role: "center",
          status: memory?.status || "active",
        });
      }
      edges.forEach(edge => {
        [edge.src_id, edge.dst_id].forEach(id => {
          if (nodes.has(id)) return;
          const memory = byMemory.get(id);
          nodes.set(id, {
            id,
            label: memory ? layerName(memory.layer) : shortId(id),
            text: memory?.text || "",
            role: id === center ? "center" : "memory",
            status: memory?.status || "active",
          });
        });
      });
      return Array.from(nodes.values()).slice(0, 40);
    }

    function mountForceGraph(hostId, rawNodes, rawEdges, options = {}) {
      const host = $(hostId);
      if (!host) return;
      const svg = host.querySelector("svg");
      const width = host.clientWidth || 900;
      const height = host.clientHeight || (options.compact ? 420 : 520);
      const centerX = width / 2;
      const centerY = height / 2;
      const nodes = rawNodes.map((node, index) => {
        const angle = (Math.PI * 2 * index / Math.max(rawNodes.length, 1)) - Math.PI / 2;
        const radius = node.role === "center" ? 0 : 110 + (index % 4) * 36;
        return {
          ...node,
          x: centerX + Math.cos(angle) * radius,
          y: centerY + Math.sin(angle) * radius,
          vx: 0,
          vy: 0,
          r: node.role === "center" ? 34 : 22,
        };
      });
      const byId = new Map(nodes.map(node => [node.id, node]));
      const edges = rawEdges
        .filter(edge => byId.has(edge.src_id) && byId.has(edge.dst_id))
        .map(edge => ({ ...edge, source: byId.get(edge.src_id), target: byId.get(edge.dst_id) }));
      const state = {
        hostId, host, svg, nodes, edges,
        zoom: 1, panX: 0, panY: 0,
        running: true, labels: true,
        strength: 1,
        raf: null,
      };
      graphInstances[hostId]?.stop?.();
      graphInstances[hostId] = state;
      state.stop = () => {
        state.running = false;
        if (state.raf) cancelAnimationFrame(state.raf);
      };
      svg.innerHTML = `<defs>
        <linearGradient id="${hostId}-center" x1="0" x2="1">
          <stop offset="0%" stop-color="#22d3ee"></stop>
          <stop offset="100%" stop-color="#a78bfa"></stop>
        </linearGradient>
        <linearGradient id="${hostId}-memory" x1="0" x2="1">
          <stop offset="0%" stop-color="#60a5fa"></stop>
          <stop offset="100%" stop-color="#a78bfa"></stop>
        </linearGradient>
      </defs>
      <g class="viewport">
        <g class="edges"></g>
        <g class="nodes"></g>
      </g>`;
      bindGraphPointers(state);
      const slider = $(`${hostId.replace("GraphHost", "")}Gravity`);
      if (slider) slider.oninput = () => { state.strength = Number(slider.value || 1); };
      drawGraph(state);
      tickGraph(state, 0);
    }

    function bindGraphPointers(state) {
      const svg = state.svg;
      let draggingNode = null;
      let panning = false;
      let last = null;
      svg.onpointerdown = event => {
        const nodeId = event.target.closest?.("[data-node-id]")?.getAttribute("data-node-id");
        last = { x: event.clientX, y: event.clientY };
        svg.setPointerCapture(event.pointerId);
        svg.classList.add("dragging");
        if (nodeId) {
          draggingNode = state.nodes.find(node => node.id === nodeId);
          if (draggingNode) draggingNode.fixed = true;
        } else {
          panning = true;
        }
      };
      svg.onpointermove = event => {
        if (!last) return;
        const dx = event.clientX - last.x;
        const dy = event.clientY - last.y;
        last = { x: event.clientX, y: event.clientY };
        if (draggingNode) {
          draggingNode.x += dx / state.zoom;
          draggingNode.y += dy / state.zoom;
          draggingNode.vx = 0;
          draggingNode.vy = 0;
          drawGraph(state);
        } else if (panning) {
          state.panX += dx;
          state.panY += dy;
          drawGraph(state);
        }
      };
      svg.onpointerup = event => {
        try { svg.releasePointerCapture(event.pointerId); } catch {}
        svg.classList.remove("dragging");
        draggingNode = null;
        panning = false;
        last = null;
      };
      svg.onwheel = event => {
        event.preventDefault();
        const next = Math.min(2.8, Math.max(.35, state.zoom * (event.deltaY > 0 ? .9 : 1.1)));
        state.zoom = next;
        drawGraph(state);
      };
    }

    function tickGraph(state, frame) {
      if (!state.running) return;
      const gravity = .004 * state.strength;
      const repulsion = 1100 * state.strength;
      for (let i = 0; i < state.nodes.length; i++) {
        const a = state.nodes[i];
        if (!a.fixed) {
          a.vx += ((state.host.clientWidth || 900) / 2 - a.x) * gravity;
          a.vy += ((state.host.clientHeight || 520) / 2 - a.y) * gravity;
        }
        for (let j = i + 1; j < state.nodes.length; j++) {
          const b = state.nodes[j];
          const dx = a.x - b.x || .01;
          const dy = a.y - b.y || .01;
          const dist2 = Math.max(80, dx * dx + dy * dy);
          const force = repulsion / dist2;
          if (!a.fixed) { a.vx += dx * force; a.vy += dy * force; }
          if (!b.fixed) { b.vx -= dx * force; b.vy -= dy * force; }
        }
      }
      state.edges.forEach(edge => {
        const a = edge.source;
        const b = edge.target;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const desired = 150 - Number(edge.weight || 0.7) * 45;
        const force = (dist - desired) * .012 * state.strength;
        const fx = dx / dist * force;
        const fy = dy / dist * force;
        if (!a.fixed) { a.vx += fx; a.vy += fy; }
        if (!b.fixed) { b.vx -= fx; b.vy -= fy; }
      });
      state.nodes.forEach(node => {
        if (node.fixed) return;
        node.vx *= .86;
        node.vy *= .86;
        node.x += node.vx;
        node.y += node.vy;
      });
      if (frame % 2 === 0) drawGraph(state);
      state.raf = requestAnimationFrame(() => tickGraph(state, frame + 1));
    }

    function drawGraph(state) {
      const viewport = state.svg.querySelector(".viewport");
      viewport.setAttribute("transform", `translate(${state.panX} ${state.panY}) scale(${state.zoom})`);
      const edgesNode = state.svg.querySelector(".edges");
      const nodesNode = state.svg.querySelector(".nodes");
      edgesNode.innerHTML = state.edges.map(edge => {
        const cls = edge.edge_type === "contradicts" || edge.edge_type === "blocks" ? "hot"
          : edge.edge_type === "supports" || edge.edge_type === "resolves" ? "ok" : "warn";
        return `<line class="edge-line ${cls}" x1="${edge.source.x}" y1="${edge.source.y}" x2="${edge.target.x}" y2="${edge.target.y}"></line>`;
      }).join("");
      nodesNode.innerHTML = state.nodes.map(node => {
        const fill = node.role === "center" ? `url(#${state.hostId}-center)`
          : node.status === "disputed" ? "#fb7185"
          : node.status === "stale" ? "#fbbf24"
          : `url(#${state.hostId}-memory)`;
        const label = escapeHtml(node.label || shortId(node.id));
        const labelY = node.y + node.r + 18;
        return `<g class="node-glow" data-node-id="${escapeHtml(node.id)}" role="button">
          <circle class="node-ring" cx="${node.x}" cy="${node.y}" r="${node.r + 7}"></circle>
          <circle class="node-core" cx="${node.x}" cy="${node.y}" r="${node.r}" fill="${fill}"></circle>
          ${state.labels ? `<text class="node-label" x="${node.x}" y="${labelY}">${label}</text>` : ""}
        </g>`;
      }).join("");
    }

    function restartGraph(hostId) {
      const state = graphInstances[hostId];
      if (!state) return;
      state.nodes.forEach((node, index) => {
        const angle = (Math.PI * 2 * index / Math.max(state.nodes.length, 1)) + Math.random() * .6;
        const radius = node.role === "center" ? 0 : 120 + Math.random() * 150;
        node.x = (state.host.clientWidth || 900) / 2 + Math.cos(angle) * radius;
        node.y = (state.host.clientHeight || 520) / 2 + Math.sin(angle) * radius;
        node.vx = 0;
        node.vy = 0;
        node.fixed = false;
      });
      drawGraph(state);
    }

    function fitGraph(hostId) {
      const state = graphInstances[hostId];
      if (!state) return;
      state.zoom = 1;
      state.panX = 0;
      state.panY = 0;
      drawGraph(state);
    }

    function toggleGraphLabels(hostId, show) {
      const state = graphInstances[hostId];
      if (!state) return;
      state.labels = show;
      drawGraph(state);
    }

    function renderOverview() {
      const memories = (lastMemories || []).slice(0, 9);
      const nodes = [
        { id: "workspace", label: "workspace", role: "center", status: "active" },
        ...memories.map(memory => ({
          id: memory.id,
          label: layerName(memory.layer),
          role: "memory",
          status: memory.status,
          text: memory.text,
        })),
      ];
      const edges = memories.map(memory => ({
        src_id: "workspace",
        dst_id: memory.id,
        edge_type: memory.status === "disputed" ? "contradicts" : "related_to",
        weight: memory.confidence || .7,
      }));
      $("overviewGraph").innerHTML = renderGraphHost("overview", "Обзорная карта памяти");
      mountForceGraph("overviewGraphHost", nodes, edges, { compact: true });
      const state = graphInstances.overviewGraphHost;
      if (!state) return;
      state.svg.querySelector(".nodes").addEventListener("click", event => {
        const nodeId = event.target.closest?.("[data-node-id]")?.getAttribute("data-node-id");
        if (nodeId && nodeId !== "workspace") selectOverviewNode(nodeId);
      });
    }

    function selectOverviewNode(id) {
      const memory = (lastMemories || []).find(row => row.id === id);
      if (!memory) return;
      $("graphItem").value = id;
      $("selectionInspector").innerHTML = `<div class="agent-row">
          <strong>${escapeHtml(layerName(memory.layer))}</strong>
          <span class="pill ok">${escapeHtml(statusName(memory.status))}</span>
        </div>
        <div class="muted tiny">${escapeHtml(memory.id)}</div>
        <p style="margin:8px 0 0">${escapeHtml(memory.text)}</p>
        <button class="secondary" style="margin-top:10px;width:100%" onclick="inspectGraph('${memory.id}')">Открыть граф узла</button>`;
    }

    function shortId(value) {
      const text = String(value);
      return text.length > 13 ? `${text.slice(0, 6)}…${text.slice(-4)}` : text;
    }

    function layerName(value) {
      return ({
        core: "ядро", working: "рабочая", semantic: "семантика", episodic: "эпизод",
        procedural: "процедура", social: "социальная", reflection: "рефлексия", error: "ошибка"
      })[value] || value;
    }

    function statusName(value) {
      return ({
        active: "активно", stale: "устарело", disputed: "спорно", rejected: "отклонено",
        archived: "архив", pinned: "закреплено", open: "открыто", accepted: "принято"
      })[value] || value;
    }

    function edgeName(value) {
      return ({
        supports: "подтверждает", contradicts: "противоречит", supersedes: "заменяет",
        derived_from: "получено из", related_to: "связано с", blocks: "блокирует",
        resolves: "решает"
      })[value] || value;
    }

    function reviewName(value) {
      return ({
        open: "открыто", accepted: "принято", rejected: "отклонено", overridden: "переопределено"
      })[value] || value;
    }

    function actionName(value) {
      return ({
        supersede: "новая ревизия",
        unchanged: "без изменений",
        conflict: "конфликт ревизии",
        skip: "пропущено",
        error: "ошибка",
      })[value] || value || "неизвестно";
    }

    function reasonName(value) {
      return ({
        "newest active value with strongest evidence; raw memories remain append-only":
          "Сервер предлагает самую свежую активную версию с сильнейшим доказательством; исходные записи остаются append-only.",
        "newer memory wins": "Побеждает более свежая запись.",
      })[value] || value || "Нет автоматического объяснения.";
    }

    refreshAll().catch(err => {
      $("kpiStatus").textContent = "Ошибка";
      $("memories").innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
      log(err.message);
    });
  </script>
</body>
</html>
"""
