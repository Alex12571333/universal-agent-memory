"""FastAPI application factory; imports remain optional for core users."""
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request as UrlRequest
from urllib.request import urlopen
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from memory_plane.adapters.documents import BinaryDocumentCommand, DocumentIngestor
from memory_plane.adapters.embeddings import (
    EmbeddingProviderConfig,
    build_embedding_client,
)
from memory_plane.adapters.llm import MemoryLLMConfig
from memory_plane.bootstrap import (
    Container,
    build_in_memory_container,
    build_postgres_container,
)
from memory_plane.build_info import BuildInfo
from memory_plane.config.database import read_database_dsn
from memory_plane.config.secrets import read_secret_env
from memory_plane.contracts.dto import (
    ContextRecipe,
    IngestDocumentCommand,
    RecallQuery,
    RetainCommand,
    SupersedeMemoryCommand,
)
from memory_plane.domain.audit import AuditEvent
from memory_plane.domain.checkpoint import Checkpoint, StaleRevisionError
from memory_plane.domain.conflict import (
    ConflictCase,
    ConflictReviewDecision,
    ConflictReviewStatus,
)
from memory_plane.domain.conversation import (
    ConversationMessage,
    ConversationRetentionPolicy,
)
from memory_plane.domain.graph import MemoryEdge, MemoryEdgeType
from memory_plane.domain.models import (
    MemoryLayer,
    MemoryRevisionConflictError,
    MemoryScope,
    MemoryStatus,
    Provenance,
)
from memory_plane.domain.proposal import MemoryProposalStatus, MemoryProposalTarget
from memory_plane.domain.worker import WorkerReadiness
from memory_plane.services.conversations import (
    AppendConversationTurnCommand,
    CurateConversationTurnCommand,
)
from memory_plane.services.identities import ProvisionIdentityCommand, ProvisionWorkspaceCommand
from memory_plane.services.metrics import render_prometheus
from memory_plane.services.proposals import (
    ReviewMemoryProposalCommand,
    SubmitMemoryProposalCommand,
)
from memory_plane.services.vault import (
    VaultImportSource,
    VaultPatchCommand,
    editable_vault_content,
)

DEFAULT_SERVER_ID = UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
DEFAULT_THREAD_ID = UUID("00000000-0000-0000-0000-000000000003")
DEFAULT_CONTEXT_BUDGET_TOKENS = int(os.getenv("UAM_CONTEXT_BUDGET_TOKENS", "8192"))
DEFAULT_CONTEXT_PER_LAYER_LIMIT = int(os.getenv("UAM_CONTEXT_PER_LAYER_LIMIT", "1000"))
PROCESS_STARTED_AT = time.time()
LOGGER = logging.getLogger(__name__)


def _audit_route_family(path: str) -> str:
    """Return a bounded route family without retaining user-controlled path data."""
    if path in {"/health", "/ready", "/metrics", "/docs", "/redoc", "/openapi.json"}:
        return path
    if path == "/ui" or path.startswith("/ui/"):
        return "/ui"
    match = re.match(
        r"^/v1/(audit|checkpoints|context|conversations|graph|identities|ingest|keys|memory|settings|system|ui|workspaces)(?:/|$)",
        path,
    )
    if match:
        return f"/v1/{match.group(1)}"
    return "/other"


def _parse_required_workers(raw: str | None) -> tuple[str, ...]:
    """Normalize an explicit fail-closed set of asynchronous worker kinds."""
    if not raw:
        return ()
    workers = tuple(
        dict.fromkeys(value.strip().lower() for value in raw.split(",") if value.strip())
    )
    invalid = [
        value
        for value in workers
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value)
    ]
    if invalid:
        raise ValueError("UAM_REQUIRED_WORKERS contains an invalid worker kind")
    return workers


def _worker_readiness_response(
    snapshot: WorkerReadiness | None,
    required_workers: tuple[str, ...],
) -> dict[str, Any]:
    """Expose aggregate liveness without process IDs, hosts or timestamps."""
    if not required_workers:
        return {
            "status": "not_configured",
            "required": [],
            "ready_count": 0,
            "missing": [],
            "stale": [],
        }
    if snapshot is None:
        return {
            "status": "unavailable",
            "required": list(required_workers),
            "ready_count": 0,
            "missing": list(required_workers),
            "stale": [],
        }
    return {
        "status": "ready" if snapshot.ready else "not_ready",
        "required": [
            {
                "kind": row.worker_kind,
                "status": "ready" if row.ready else "not_ready",
                "fresh_instances": row.fresh_instances,
                "stale_instances": row.stale_instances,
            }
            for row in snapshot.required
        ],
        "ready_count": sum(row.ready for row in snapshot.required),
        "missing": list(snapshot.missing_kinds),
        "stale": list(snapshot.stale_kinds),
    }


def _web_dist_dir() -> Path:
    """Return the built React dashboard directory, if present."""
    configured = os.getenv("UAM_WEB_DIST")
    if configured:
        return Path(configured)
    return Path.cwd() / "web" / "dist"


def _process_rss_mb() -> float | None:
    """Return real process max RSS in MiB when the platform exposes it."""
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, OSError):
        return None
    if sys.platform == "darwin":
        return round(rss / 1024 / 1024, 1)
    return round(rss / 1024, 1)


def _disk_usage_response(path: str = "/app") -> dict[str, Any]:
    """Return real disk usage for the server container/workdir."""
    target = path if os.path.exists(path) else os.getcwd()
    usage = shutil.disk_usage(target)
    return {
        "path": target,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else None,
    }


def _private_dependency_health(url: str) -> dict[str, str]:
    """Probe a fixed operator-configured dependency without leaking its URL."""
    if os.getenv("UAM_RUNTIME_DEPENDENCY_PROBES", "false").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return {"status": "not_configured"}
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return {"status": "misconfigured"}
        request = UrlRequest(url, method="GET")
        with urlopen(request, timeout=0.5) as response:  # noqa: S310 - operator config only.
            return {"status": "healthy" if response.status == 200 else "unhealthy"}
    except (OSError, URLError, ValueError):
        return {"status": "unavailable"}


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
    top_k: int = Field(default=12, ge=1, le=1000)
    minimum_score: float = Field(default=0, ge=0, le=1)
    operation: str = "chat_reply"
    context_budget_tokens: int = Field(default=DEFAULT_CONTEXT_BUDGET_TOKENS, ge=128)


class UiSessionLoginBody(BaseModel):
    """One-time operator credential exchange for an HttpOnly browser session."""

    api_key: str = Field(min_length=1, max_length=8192)


class ConversationMessageBody(BaseModel):
    """One raw transcript message."""

    role: str = Field(min_length=1)
    content: str = Field(min_length=1)
    name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IdentityProvisionBody(BaseModel):
    """Operator request to provision one stable agent and optional thread."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    workspace_id: UUID = DEFAULT_PROJECT_ID
    agent_id: UUID
    agent_name: str = Field(min_length=1, max_length=160)
    agent_role: str = Field(min_length=1, max_length=80)
    agent_config: dict[str, Any] = Field(default_factory=dict)
    thread_id: UUID | None = None
    thread_status: Literal["active", "closed", "archived"] = "active"


class WorkspaceProvisionBody(BaseModel):
    """Operator request to provision a workspace before agent registration."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    workspace_id: UUID
    workspace_name: str = Field(min_length=1, max_length=160)


class ConversationTurnBody(BaseModel):
    """Append one immutable raw conversation turn."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    workspace_id: UUID = DEFAULT_PROJECT_ID
    thread_id: UUID = DEFAULT_THREAD_ID
    namespace: str = Field(default="default", min_length=1)
    agent_id: UUID | None = None
    source_kind: str = Field(default="api", min_length=1)
    retention_policy: ConversationRetentionPolicy = ConversationRetentionPolicy.RAW_AND_CURATED
    messages: list[ConversationMessageBody] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class CurateConversationTurnBody(BaseModel):
    """Create curated memory from one raw transcript turn.

    The default enables the narrow evidence-grounded auto-policy.  It accepts
    only high-confidence, source-quoted, non-temporal proposal types; every
    other result remains an open proposal for review.
    """

    tenant_id: UUID = DEFAULT_SERVER_ID
    layer: MemoryLayer = MemoryLayer.EPISODIC
    kind: str = "conversation_summary"
    labels: list[str] = Field(default_factory=list)
    importance: float = Field(default=0.4, ge=0, le=1)
    confidence: float = Field(default=0.65, ge=0, le=1)
    auto_accept: bool = True
    idempotency_key: str | None = None


class MemoryProposalBody(BaseModel):
    """Submit one proposed memory update through Memory Gateway."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    workspace_id: UUID = DEFAULT_PROJECT_ID
    namespace: str = Field(default="default", min_length=1)
    requester: str = Field(default="memory-gateway", min_length=1)
    proposal: str = Field(min_length=1)
    evidence: str = ""
    target: MemoryProposalTarget = MemoryProposalTarget.AUTO
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    confidence: float = Field(default=0.7, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class MemoryProposalReviewBody(BaseModel):
    """Accept or reject one memory proposal."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    reviewer: str = Field(default="operator", min_length=1)
    reason: str = ""
    layer: MemoryLayer | None = None
    kind: str | None = None
    labels: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None


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


class ApiKeyRevokeBody(BaseModel):
    """Operator request to revoke one configured API key."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    reason: str = ""


class CheckpointCompactBody(BaseModel):
    """Compaction request body."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    workspace_id: UUID = DEFAULT_PROJECT_ID
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


def _audit_event_response(event: Any) -> dict[str, Any]:
    """Render an audit event without exposing internal Python objects."""
    return {
        "id": str(event.id),
        "tenant_id": str(event.tenant_id),
        "workspace_id": str(event.workspace_id) if event.workspace_id else None,
        "action": event.action,
        "actor": event.actor,
        "actor_type": event.actor_type,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "status": event.status,
        "metadata": event.metadata,
        "created_at": event.created_at.isoformat(),
    }


def _api_key_response(record: Any) -> dict[str, Any]:
    """Render API-key metadata without exposing bearer secrets."""
    fingerprint = str(record.secret_fingerprint)
    return {
        "id": str(record.id),
        "tenant_id": str(record.tenant_id),
        "name": record.name,
        "fingerprint": f"{fingerprint[:12]}…{fingerprint[-6:]}",
        "scopes": list(record.scopes),
        "created_at": record.created_at.isoformat(),
        "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
        "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
        "revoked": bool(record.revoked),
        "revoked_reason": record.revoked_reason,
    }


def _memory_write_response(result: Any) -> dict[str, Any]:
    """Render a write result with revision metadata needed for CAS clients."""
    return {
        "id": str(result.item.id),
        "created": result.created,
        "revision": result.item.revision,
        "supersedes_id": (
            str(result.item.supersedes_id) if result.item.supersedes_id is not None else None
        ),
        "queued_event_ids": [str(event_id) for event_id in result.queued_event_ids],
    }


def _conversation_turn_response(turn: Any, *, created: bool | None = None) -> dict[str, Any]:
    """Render a raw conversation turn without treating it as prompt context."""
    payload = {
        "id": str(turn.id),
        "tenant_id": str(turn.tenant_id),
        "workspace_id": str(turn.workspace_id),
        "thread_id": str(turn.thread_id),
        "agent_id": str(turn.agent_id) if turn.agent_id else None,
        "namespace": turn.namespace,
        "source_kind": turn.source_kind,
        "retention_policy": turn.retention_policy.value,
        "metadata": turn.metadata,
        "created_at": turn.created_at.isoformat(),
        "expires_at": turn.expires_at.isoformat() if turn.expires_at else None,
        "messages": [
            {
                "role": message.role,
                "name": message.name,
                "content": message.content,
                "metadata": message.metadata,
            }
            for message in turn.messages
        ],
    }
    if created is not None:
        payload["created"] = created
    return payload


def _memory_proposal_response(proposal: Any, *, created: bool | None = None) -> dict[str, Any]:
    """Render a Memory Gateway proposal."""
    payload = {
        "id": str(proposal.id),
        "tenant_id": str(proposal.tenant_id),
        "workspace_id": str(proposal.workspace_id),
        "agent_id": str(proposal.agent_id) if proposal.agent_id else None,
        "thread_id": str(proposal.thread_id) if proposal.thread_id else None,
        "namespace": proposal.namespace,
        "requester": proposal.requester,
        "target": proposal.target.value,
        "proposal": proposal.proposal,
        "evidence": proposal.evidence,
        "confidence": proposal.confidence,
        "importance": proposal.importance,
        "status": proposal.status.value,
        "metadata": proposal.metadata,
        "created_at": proposal.created_at.isoformat(),
        "reviewed_at": proposal.reviewed_at.isoformat() if proposal.reviewed_at else None,
        "reviewer": proposal.reviewer,
        "review_reason": proposal.review_reason,
    }
    if created is not None:
        payload["created"] = created
    return payload


def _memory_proposal_review_response(result: Any) -> dict[str, Any]:
    """Render proposal review result and optional retained memory."""
    return {
        "proposal": _memory_proposal_response(result.proposal),
        "memory": _memory_write_response(result.retained) if result.retained is not None else None,
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


class VaultDeleteBody(BaseModel):
    """Archive one human-editable memory note from the vault UI."""

    tenant_id: UUID = DEFAULT_SERVER_ID
    file: VaultImportFileBody


class VaultPatchSectionBody(BaseModel):
    """Replace the content of one existing human-owned Markdown section."""

    model_config = ConfigDict(extra="forbid")

    heading: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=1_000_000)


class VaultPatchBody(BaseModel):
    """Targeted append-only edit of one canonical memory head."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID = DEFAULT_SERVER_ID
    expected_revision: int = Field(ge=1)
    replace_body: str | None = Field(default=None, max_length=1_000_000)
    replace_section: VaultPatchSectionBody | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


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

    provider: Literal["fake", "openai-compatible", "openai", "ollama", "tei"] = "fake"
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
        "applied_memory_id": str(decision.applied_memory_id)
        if decision.applied_memory_id
        else None,
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
        "provenance_item_id": (str(edge.provenance_item_id) if edge.provenance_item_id else None),
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
    if not isinstance(data, dict):
        return None
    if "api_key" in data:
        data.pop("api_key", None)
        _save_model_settings(data)
    return data


def _save_model_settings(settings: dict[str, Any]) -> None:
    """Atomically persist non-secret desired settings with owner-only permissions."""
    path = _settings_path()
    if not path:
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    persisted = {key: value for key, value in settings.items() if key != "api_key"}
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(persisted, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _model_endpoint_origin(base_url: str) -> str:
    """Normalize an HTTP(S) endpoint to an exact scheme/host/port origin."""
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("model base URL must use http or https with a hostname")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("model base URL must not contain credentials, query or fragment")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = parsed.hostname.casefold()
    formatted_host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{formatted_host}:{port}"


def _assert_model_endpoint_allowed(settings: dict[str, Any]) -> None:
    """Enforce an exact-origin egress policy before saving or probing an endpoint."""
    if settings["provider"] == "fake":
        return
    base_url = str(settings.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("model base URL is required for this provider")
    origin = _model_endpoint_origin(base_url)
    configured = os.getenv("UAM_MODEL_ENDPOINT_ALLOWLIST", "")
    allowed_origins = {
        _model_endpoint_origin(entry)
        for entry in configured.split(",")
        if entry.strip()
    }
    if allowed_origins:
        if origin not in allowed_origins:
            raise ValueError(f"model endpoint origin {origin!r} is not in the allowlist")
        return
    hostname = urlsplit(base_url).hostname or ""
    is_loopback = hostname.casefold() == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if not is_loopback:
        raise ValueError(
            "remote model endpoints require UAM_MODEL_ENDPOINT_ALLOWLIST"
        )


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
    restart_required = (
        desired_config["provider"] != env_config.provider
        or desired_config["model_name"] != env_config.model_name
        or int(desired_config["dimension"]) != env_config.dimension
        or (desired_config.get("base_url") or None) != env_config.base_url
        or float(desired_config["timeout_seconds"]) != env_config.timeout_seconds
    )
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
        "restart_required": restart_required,
        "env": {
            "UAM_EMBEDDING_PROVIDER": desired_config["provider"],
            "UAM_EMBEDDING_MODEL": desired_config["model_name"],
            "UAM_EMBEDDING_DIM": str(desired_config["dimension"]),
            "UAM_EMBEDDING_BASE_URL": desired_config.get("base_url") or "",
            "UAM_EMBEDDING_SEND_DIMENSIONS": (
                "true" if desired_config["provider"] == "openai" else "false"
            ),
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


@dataclass(frozen=True, slots=True)
class ApiPrincipal:
    """Authenticated API principal derived from a bearer token."""

    name: str
    scopes: frozenset[str]
    secret_fingerprint: str | None = None
    tenant_id: UUID | None = None
    workspace_id: UUID | None = None
    agent_id: UUID | None = None

    def has_scope(self, scope: str) -> bool:
        return "admin" in self.scopes or "operator" in self.scopes or scope in self.scopes


UI_SESSION_COOKIE = "uam_ui_session"


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _issue_ui_session(
    principal: ApiPrincipal,
    signing_key: str,
    *,
    ttl_seconds: int,
) -> tuple[str, str, int]:
    """Create a signed bearer-free browser session and its CSRF token."""
    now = int(time.time())
    expires_at = now + ttl_seconds
    csrf_token = secrets.token_urlsafe(32)
    payload = {
        "sub": principal.name,
        "fp": principal.secret_fingerprint,
        "iat": now,
        "exp": expires_at,
        "csrf": csrf_token,
    }
    encoded = _base64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _base64url_encode(
        hmac.new(signing_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{encoded}.{signature}", csrf_token, expires_at


def _verify_ui_session(token: str, signing_key: str) -> dict[str, Any] | None:
    """Verify signature, shape and expiry of a browser session cookie."""
    try:
        encoded, signature = token.split(".", 1)
        expected = _base64url_encode(
            hmac.new(
                signing_key.encode("utf-8"),
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_base64url_decode(encoded))
        if not isinstance(payload, dict):
            return None
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
        if not all(isinstance(payload.get(field), str) for field in ("sub", "fp", "csrf")):
            return None
        return payload
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None


@dataclass(frozen=True, slots=True)
class PrincipalBinding:
    """Stable authorization boundary attached to one configured principal."""

    tenant_id: UUID
    workspace_id: UUID
    agent_id: UUID


def _parse_principal_bindings(raw: str | None) -> dict[str, PrincipalBinding]:
    """Parse non-secret identity bindings keyed by configured API principal name."""
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("UAM_API_PRINCIPAL_BINDINGS_JSON must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("UAM_API_PRINCIPAL_BINDINGS_JSON must be a JSON object")
    bindings: dict[str, PrincipalBinding] = {}
    for name, value in payload.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(value, dict):
            raise ValueError("each API principal binding must be a named JSON object")
        try:
            bindings[name.strip()] = PrincipalBinding(
                tenant_id=UUID(str(value["tenant_id"])),
                workspace_id=UUID(str(value["workspace_id"])),
                agent_id=UUID(str(value["agent_id"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"binding for {name!r} requires tenant_id, workspace_id and agent_id UUIDs"
            ) from exc
    return bindings


def _parse_scoped_api_keys(raw: str | None) -> tuple[tuple[str, str, frozenset[str]], ...]:
    """Parse UAM_API_KEYS entries: name:secret:scope+scope,name2:secret2:admin."""
    if not raw:
        return ()
    parsed: list[tuple[str, str, frozenset[str]]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            name, secret, scopes_raw = entry.split(":", 2)
        except ValueError:
            continue
        scopes = frozenset(
            scope.strip().lower()
            for scope in scopes_raw.replace("|", "+").split("+")
            if scope.strip()
        )
        if name.strip() and secret.strip() and scopes:
            parsed.append((name.strip(), secret.strip(), scopes))
    return tuple(parsed)


def _secret_fingerprint(secret: str) -> str:
    """Return a stable non-secret fingerprint for a bearer token."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _registry_tenant_id() -> UUID:
    """Return the deployment tenant used for auth/key metadata."""
    return UUID(os.getenv("UAM_SERVER_ID", str(DEFAULT_SERVER_ID)))


def _required_scope_for_request(path: str, method: str) -> str:
    """Return the minimum logical scope required for a route."""
    if path in {"/health", "/ready"} or path.startswith("/ui"):
        return "public"
    if path == "/v1/ui/session":
        return "public"
    if path.startswith("/v1/keys"):
        return "operator"
    if path.startswith("/v1/audit"):
        return "operator"
    if path.startswith("/v1/identities"):
        return "operator"
    if path == "/v1/workspaces/provision":
        return "operator"
    if path.startswith("/v1/graph"):
        return "operator"
    if path.startswith("/v1/workspaces/"):
        if path.endswith("/seed") and method == "GET":
            return "read"
        if path.endswith("/reflect") and method == "POST":
            return "write"
        return "operator"
    if path.startswith("/v1/memory/") and path.endswith("/supersede"):
        return "operator"
    if path.startswith("/v1/memory/") and path.endswith("/neighbors"):
        return "operator"
    if path.startswith("/v1/memory/proposals"):
        if path == "/v1/memory/proposals" and method == "POST":
            return "write"
        return "operator"
    if path.startswith("/v1/conversations"):
        if path == "/v1/conversations/turns" and method == "POST":
            return "write"
        return "operator"
    if path == "/v1/checkpoints" and method == "GET":
        return "operator"
    if path.startswith("/v1/checkpoints/") and path.endswith("/compact"):
        return "operator"
    if path.startswith(("/docs", "/redoc", "/openapi.json", "/metrics")):
        return "operator"
    if path.startswith(("/v1/system", "/v1/settings")):
        return "operator"
    if path.endswith("/recall") or path.startswith("/v1/context"):
        return "read"
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "read"
    return "write"


def _scope_allowed(principal: ApiPrincipal, required_scope: str) -> bool:
    if required_scope == "public":
        return True
    if principal.has_scope(required_scope):
        return True
    if required_scope in {"read", "write"} and principal.has_scope("agent"):
        return True
    return False


def _uuid_value(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value not in (None, "") else None
    except ValueError:
        return None


async def _agent_binding_error(
    request: Request,
    principal: ApiPrincipal,
    store: object,
    *,
    require_binding: bool,
) -> str | None:
    """Return a denial reason when an agent request crosses its bound identity."""
    if "agent" not in principal.scopes:
        return None
    if principal.tenant_id is None or principal.workspace_id is None or principal.agent_id is None:
        return "agent API key has no identity binding" if require_binding else None

    payload: dict[str, Any] = {}
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and "json" in request.headers.get(
        "content-type", ""
    ).lower():
        try:
            decoded = json.loads((await request.body()) or b"{}")
            if isinstance(decoded, dict):
                payload = decoded
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def supplied(name: str) -> object | None:
        return payload.get(name, request.query_params.get(name))

    tenant_id = _uuid_value(supplied("tenant_id")) or DEFAULT_SERVER_ID
    workspace_id = _uuid_value(supplied("workspace_id")) or DEFAULT_PROJECT_ID
    workspace_match = re.match(r"^/v1/workspaces/([0-9a-fA-F-]{36})(?:/|$)", request.url.path)
    if workspace_match:
        path_workspace_id = _uuid_value(workspace_match.group(1))
        if path_workspace_id is None:
            return "workspace path contains an invalid UUID"
        workspace_id = path_workspace_id
    if tenant_id != principal.tenant_id:
        return "tenant_id is outside the API principal binding"
    if workspace_id != principal.workspace_id:
        return "workspace_id is outside the API principal binding"

    supplied_agent = _uuid_value(supplied("agent_id"))
    if supplied_agent is not None and supplied_agent != principal.agent_id:
        return "agent_id is outside the API principal binding"
    attributed_write = request.method.upper() == "POST" and (
        request.url.path == "/v1/memory/retain"
        or request.url.path == "/v1/conversations/turns"
        or request.url.path == "/v1/memory/proposals"
        or request.url.path.startswith("/v1/ingest/")
    )
    if attributed_write and supplied_agent is None:
        return "agent_id is required for an agent-authenticated write"

    thread_id = _uuid_value(supplied("thread_id"))
    checkpoint_match = re.match(r"^/v1/checkpoints/([0-9a-fA-F-]{36})(?:/|$)", request.url.path)
    if checkpoint_match:
        thread_id = _uuid_value(checkpoint_match.group(1))
        if thread_id is None:
            return "checkpoint path contains an invalid thread UUID"
    if thread_id is not None:
        checker = getattr(store, "thread_belongs_to_agent", None)
        owned = bool(
            callable(checker)
            and await asyncio.to_thread(
                checker,
                principal.tenant_id,
                principal.workspace_id,
                principal.agent_id,
                thread_id,
            )
        )
        if not owned:
            return "thread_id is not owned by the API principal"
    return None


def _principal_from_request(request: Request) -> ApiPrincipal:
    """Return the authenticated principal, or an explicit local-dev actor."""
    principal = getattr(request.state, "api_principal", None)
    if isinstance(principal, ApiPrincipal):
        return principal
    return ApiPrincipal(name="local-dev", scopes=frozenset({"admin"}))


def _audit_actor_type(principal: ApiPrincipal) -> str:
    """Classify the principal for operator review."""
    if "agent" in principal.scopes:
        return "agent"
    if principal.has_scope("operator") or principal.has_scope("admin"):
        return "operator"
    return "api_key"


def _apply_security_headers(response: Any) -> Any:
    """Add safe default browser/API security headers."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'",
    )
    return response


def create_app(
    container: Container | None = None,
    *,
    api_key: str | None = None,
) -> FastAPI:
    """Create the standalone memory server around an injected service graph."""
    services = container or _build_runtime_container()
    documents = DocumentIngestor(services.ingestion)
    configured_key = api_key if api_key is not None else read_secret_env("UAM_API_KEY")
    configured_scoped_keys = _parse_scoped_api_keys(read_secret_env("UAM_API_KEYS"))
    principal_bindings = _parse_principal_bindings(
        read_secret_env("UAM_API_PRINCIPAL_BINDINGS_JSON")
    )
    require_identity_bindings = os.getenv(
        "UAM_REQUIRE_IDENTITY_BINDINGS",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    ui_session_signing_key = read_secret_env("UAM_UI_SESSION_SIGNING_KEY")
    if ui_session_signing_key is not None and len(ui_session_signing_key) < 32:
        raise ValueError("UAM_UI_SESSION_SIGNING_KEY must contain at least 32 characters")
    try:
        ui_session_ttl_seconds = int(os.getenv("UAM_UI_SESSION_TTL_SECONDS", "28800"))
    except ValueError as exc:
        raise ValueError("UAM_UI_SESSION_TTL_SECONDS must be an integer") from exc
    if not 300 <= ui_session_ttl_seconds <= 86400:
        raise ValueError("UAM_UI_SESSION_TTL_SECONDS must be between 300 and 86400")
    ui_cookie_secure = os.getenv("UAM_UI_COOKIE_SECURE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if require_identity_bindings:
        missing_bindings = sorted(
            name
            for name, _secret, scopes in configured_scoped_keys
            if "agent" in scopes and name not in principal_bindings
        )
        if missing_bindings:
            raise RuntimeError(
                "agent API principals require identity bindings: "
                + ", ".join(missing_bindings)
            )
    auth_tenant_id = _registry_tenant_id()
    required_workers = _parse_required_workers(os.getenv("UAM_REQUIRED_WORKERS"))
    try:
        worker_heartbeat_ttl_seconds = int(
            os.getenv("UAM_WORKER_HEARTBEAT_TTL_SECONDS", "30")
        )
    except ValueError as exc:
        raise ValueError("UAM_WORKER_HEARTBEAT_TTL_SECONDS must be an integer") from exc
    if not 5 <= worker_heartbeat_ttl_seconds <= 600:
        raise ValueError(
            "UAM_WORKER_HEARTBEAT_TTL_SECONDS must be between 5 and 600"
        )
    model_settings = _load_model_settings()
    build_info = BuildInfo.from_env()
    app = FastAPI(
        title="Obelisk Memory Server",
        version=build_info.version,
        description="Self-hosted memory API for local and team AI agents.",
    )
    web_dist = _web_dist_dir()
    web_assets = web_dist / "assets"
    if web_assets.exists():
        app.mount(
            "/ui/assets",
            StaticFiles(directory=str(web_assets)),
            name="operator-ui-assets",
        )

    def sync_configured_api_keys() -> None:
        """Mirror configured env keys into the non-secret key registry."""
        if configured_key:
            services.api_keys.ensure_configured_key(
                auth_tenant_id,
                name="server",
                secret_fingerprint=_secret_fingerprint(configured_key),
                scopes=("admin",),
            )
        for name, secret, scopes in configured_scoped_keys:
            services.api_keys.ensure_configured_key(
                auth_tenant_id,
                name=name,
                secret_fingerprint=_secret_fingerprint(secret),
                scopes=tuple(sorted(scopes)),
            )

    sync_configured_api_keys()

    def principal_for_credential(credential: str) -> ApiPrincipal | None:
        """Resolve a configured bearer secret without exposing it to browser storage."""
        if configured_key and secrets.compare_digest(credential, configured_key):
            return ApiPrincipal(
                name="server",
                scopes=frozenset({"admin"}),
                secret_fingerprint=_secret_fingerprint(credential),
            )
        for name, secret, scopes in configured_scoped_keys:
            if secrets.compare_digest(credential, secret):
                binding = principal_bindings.get(name)
                return ApiPrincipal(
                    name=name,
                    scopes=scopes,
                    secret_fingerprint=_secret_fingerprint(credential),
                    tenant_id=binding.tenant_id if binding else None,
                    workspace_id=binding.workspace_id if binding else None,
                    agent_id=binding.agent_id if binding else None,
                )
        return None

    def principal_for_session_claims(claims: dict[str, Any]) -> ApiPrincipal | None:
        """Re-resolve a session against current key configuration on every request."""
        name = str(claims.get("sub", ""))
        fingerprint = str(claims.get("fp", ""))
        if name == "server" and configured_key:
            principal = principal_for_credential(configured_key)
            if principal and secrets.compare_digest(
                fingerprint,
                principal.secret_fingerprint or "",
            ):
                return principal
        for configured_name, secret, _scopes in configured_scoped_keys:
            if configured_name != name:
                continue
            principal = principal_for_credential(secret)
            if principal and secrets.compare_digest(
                fingerprint,
                principal.secret_fingerprint or "",
            ):
                return principal
        return None

    def revoked(principal: ApiPrincipal) -> bool:
        if not principal.secret_fingerprint:
            return False
        record = services.api_keys.get_by_fingerprint(
            auth_tenant_id,
            principal.secret_fingerprint,
        )
        return bool(record is not None and record.revoked)

    def browser_session(request: Request) -> tuple[ApiPrincipal, dict[str, Any]] | None:
        if not ui_session_signing_key:
            return None
        token = request.cookies.get(UI_SESSION_COOKIE, "")
        claims = _verify_ui_session(token, ui_session_signing_key)
        if claims is None:
            return None
        principal = principal_for_session_claims(claims)
        if principal is None or revoked(principal):
            return None
        return principal, claims

    def record_auth_denial(
        request: Request,
        *,
        reason: str,
        required_scope: str,
        principal: ApiPrincipal | None = None,
    ) -> None:
        """Persist a bounded denial record without request content or credentials."""
        route_family = _audit_route_family(request.url.path)
        try:
            services.audit.record(
                tenant_id=principal.tenant_id if principal and principal.tenant_id else auth_tenant_id,
                workspace_id=principal.workspace_id if principal else None,
                action="auth.request.denied",
                actor=principal.name if principal else "anonymous",
                actor_type=_audit_actor_type(principal) if principal else "unauthenticated",
                resource_type="api_route",
                resource_id=route_family,
                status="denied",
                metadata={
                    "method": request.method.upper(),
                    "route_family": route_family,
                    "required_scope": required_scope,
                    "reason": reason,
                },
            )
        except Exception:
            # The authorization decision remains fail-closed even if the audit
            # store is unavailable. Never log the database exception here: a
            # provider error can contain endpoint or credential material.
            LOGGER.error(
                "failed to persist authorization denial audit event",
                extra={"route_family": route_family, "reason": reason},
            )

    async def denied_response(
        request: Request,
        *,
        status_code: int,
        content: dict[str, Any],
        reason: str,
        required_scope: str,
        principal: ApiPrincipal | None = None,
        headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        """Record an authorization denial before returning its safe response."""
        await asyncio.to_thread(
            record_auth_denial,
            request,
            reason=reason,
            required_scope=required_scope,
            principal=principal,
        )
        return _apply_security_headers(
            JSONResponse(status_code=status_code, content=content, headers=headers)
        )

    @app.middleware("http")
    async def require_api_key(request: Request, call_next: Any) -> Any:
        """Protect every endpoint except liveness when API keys are configured."""
        required_scope = _required_scope_for_request(request.url.path, request.method.upper())
        if required_scope == "public":
            return _apply_security_headers(await call_next(request))
        if not configured_key and not configured_scoped_keys:
            return _apply_security_headers(await call_next(request))
        authorization = request.headers.get("Authorization", "")
        scheme, _, credential = authorization.partition(" ")
        principal: ApiPrincipal | None = None
        session_claims: dict[str, Any] | None = None
        if scheme.casefold() == "bearer":
            principal = principal_for_credential(credential)
        else:
            session = browser_session(request)
            if session is not None:
                principal, session_claims = session
        if principal is None:
            return await denied_response(
                request,
                status_code=401,
                content={"detail": "invalid or missing API key"},
                headers={"WWW-Authenticate": "Bearer"},
                reason=(
                    "invalid_credential"
                    if authorization or request.cookies.get(UI_SESSION_COOKIE)
                    else "missing_credential"
                ),
                required_scope=required_scope,
            )
        if revoked(principal):
            return await denied_response(
                request,
                status_code=401,
                content={"detail": "API key has been revoked"},
                headers={"WWW-Authenticate": "Bearer"},
                reason="revoked_credential",
                required_scope=required_scope,
                principal=principal,
            )
        if not _scope_allowed(principal, required_scope):
            return await denied_response(
                request,
                status_code=403,
                content={
                    "detail": "API key scope is insufficient",
                    "required_scope": required_scope,
                },
                reason="insufficient_scope",
                required_scope=required_scope,
                principal=principal,
            )
        if session_claims is not None and request.method.upper() not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            supplied_csrf = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(supplied_csrf, str(session_claims["csrf"])):
                return await denied_response(
                    request,
                    status_code=403,
                    content={"detail": "missing or invalid CSRF token"},
                    reason="csrf_validation_failed",
                    required_scope=required_scope,
                    principal=principal,
                )
        binding_error = await _agent_binding_error(
            request,
            principal,
            services.store,
            require_binding=require_identity_bindings,
        )
        if binding_error is not None:
            return await denied_response(
                request,
                status_code=403,
                content={
                    "detail": binding_error,
                    "error": "identity_boundary_violation",
                },
                reason="identity_boundary_violation",
                required_scope=required_scope,
                principal=principal,
            )
        request.state.api_principal = principal
        request.state.ui_session_claims = session_claims
        if principal.secret_fingerprint:
            services.api_keys.touch(auth_tenant_id, principal.secret_fingerprint)
        return _apply_security_headers(await call_next(request))

    def record_audit(
        request: Request,
        *,
        tenant_id: UUID,
        workspace_id: UUID | None,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist one audit event for a successful API-side action."""
        principal = _principal_from_request(request)
        services.audit.record(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            action=action,
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata or {},
        )

    def worker_readiness_snapshot() -> WorkerReadiness | None:
        """Load aggregate worker state or fail closed when the probe is unavailable."""
        if not required_workers:
            return WorkerReadiness()
        probe = getattr(services.store, "worker_readiness", None)
        if not callable(probe):
            return None
        try:
            return probe(
                auth_tenant_id,
                required_workers,
                stale_after_seconds=worker_heartbeat_ttl_seconds,
            )
        except Exception:
            return None

    @app.get("/health")
    def health() -> dict[str, str]:
        """Report process liveness; adapters should extend readiness separately."""
        return {"status": "ok"}

    @app.get("/ready")
    def readiness() -> JSONResponse:
        """Report canonical-store readiness and optional retrieval degradation."""
        ping = getattr(services.store, "ping", None)
        try:
            canonical_ready = bool(callable(ping) and ping())
        except Exception:
            canonical_ready = False
        sources = services.retrieval.source_health()
        worker_snapshot = worker_readiness_snapshot() if canonical_ready else None
        workers_ready = not required_workers or bool(worker_snapshot and worker_snapshot.ready)
        status = "ready" if canonical_ready and workers_ready else "not_ready"
        if (
            canonical_ready
            and workers_ready
            and any(row["status"] == "degraded" for row in sources.values())
        ):
            status = "degraded"
        return JSONResponse(
            status_code=200 if canonical_ready and workers_ready else 503,
            content={
                "status": status,
                # Release identity is deliberately public: agents and probes must
                # be able to bind their evidence to the exact local deployment
                # without receiving operator-only process telemetry.
                "version": app.version,
                "build": build_info.public_dict(),
                "canonical_store": "healthy" if canonical_ready else "failed",
                "retrieval_sources": sources,
                "worker_pipeline": _worker_readiness_response(
                    worker_snapshot,
                    required_workers,
                ),
            },
        )

    @app.post("/v1/ui/session")
    def create_ui_session(body: UiSessionLoginBody, request: Request) -> JSONResponse:
        """Exchange an operator key for a signed HttpOnly same-origin session."""
        if not configured_key and not configured_scoped_keys:
            return JSONResponse(
                {"authenticated": True, "auth_required": False, "csrf_token": None}
            )
        if not ui_session_signing_key:
            raise HTTPException(
                status_code=503,
                detail="browser sessions are not configured",
            )
        principal = principal_for_credential(body.api_key)
        if principal is None:
            record_auth_denial(
                request,
                reason="invalid_credential",
                required_scope="operator",
            )
            raise HTTPException(status_code=401, detail="invalid operator credential")
        if revoked(principal):
            record_auth_denial(
                request,
                reason="revoked_credential",
                required_scope="operator",
                principal=principal,
            )
            raise HTTPException(status_code=401, detail="invalid operator credential")
        if not _scope_allowed(principal, "operator"):
            record_auth_denial(
                request,
                reason="insufficient_scope",
                required_scope="operator",
                principal=principal,
            )
            raise HTTPException(status_code=403, detail="operator scope is required")
        token, csrf_token, expires_at = _issue_ui_session(
            principal,
            ui_session_signing_key,
            ttl_seconds=ui_session_ttl_seconds,
        )
        services.audit.record(
            tenant_id=auth_tenant_id,
            workspace_id=None,
            action="ui.session.login",
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type="ui_session",
            resource_id=principal.secret_fingerprint,
            metadata={"expires_at": expires_at},
        )
        response = JSONResponse(
            {
                "authenticated": True,
                "auth_required": True,
                "principal": principal.name,
                "csrf_token": csrf_token,
                "expires_at": expires_at,
            }
        )
        response.set_cookie(
            UI_SESSION_COOKIE,
            token,
            max_age=ui_session_ttl_seconds,
            expires=ui_session_ttl_seconds,
            path="/",
            secure=ui_cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/v1/ui/session")
    def get_ui_session(request: Request) -> dict[str, Any]:
        """Bootstrap React auth state without exposing the original API key."""
        if not configured_key and not configured_scoped_keys:
            return {
                "authenticated": True,
                "auth_required": False,
                "principal": "local-dev",
                "csrf_token": None,
            }
        session = browser_session(request)
        if session is None:
            return {"authenticated": False, "auth_required": True}
        principal, claims = session
        if not _scope_allowed(principal, "operator"):
            return {"authenticated": False, "auth_required": True}
        return {
            "authenticated": True,
            "auth_required": True,
            "principal": principal.name,
            "csrf_token": claims["csrf"],
            "expires_at": claims["exp"],
        }

    @app.delete("/v1/ui/session")
    def delete_ui_session(request: Request) -> JSONResponse:
        """Invalidate the browser cookie; key rotation/revocation invalidates it server-side."""
        session = browser_session(request)
        if session is not None:
            principal, claims = session
            supplied_csrf = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(supplied_csrf, str(claims["csrf"])):
                record_auth_denial(
                    request,
                    reason="csrf_validation_failed",
                    required_scope="operator",
                    principal=principal,
                )
                raise HTTPException(status_code=403, detail="missing or invalid CSRF token")
            services.audit.record(
                tenant_id=auth_tenant_id,
                workspace_id=None,
                action="ui.session.logout",
                actor=principal.name,
                actor_type=_audit_actor_type(principal),
                resource_type="ui_session",
                resource_id=principal.secret_fingerprint,
            )
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(
            UI_SESSION_COOKIE,
            path="/",
            secure=ui_cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/v1/audit/events")
    def list_audit_events(
        tenant_id: UUID = DEFAULT_SERVER_ID,
        workspace_id: UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        limit: int = 100,
        before_created_at: datetime | None = None,
        before_event_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Export recent audit events for operator review and incident response."""
        safe_limit = max(1, min(int(limit), 500))
        events = services.audit.list_events(
            tenant_id,
            workspace_id=workspace_id,
            action=action,
            resource_type=resource_type,
            created_before=before_created_at,
            before_event_id=before_event_id,
            limit=safe_limit + 1,
        )
        has_more = len(events) > safe_limit
        rows = events[:safe_limit]
        cursor = rows[-1] if has_more and rows else None
        return {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id) if workspace_id else None,
            "count": len(rows),
            "limit": safe_limit,
            "has_more": has_more,
            "next_before_created_at": cursor.created_at.isoformat() if cursor else None,
            "next_before_event_id": str(cursor.id) if cursor else None,
            "events": [_audit_event_response(event) for event in rows],
        }

    @app.get("/v1/workspaces/{workspace_id}/replays/{audit_event_id}")
    def get_recall_replay(
        workspace_id: UUID,
        audit_event_id: UUID,
        tenant_id: UUID = DEFAULT_SERVER_ID,
    ) -> dict[str, Any]:
        """Explain one recall using redacted audit data and canonical memory IDs."""
        try:
            replay = services.replay.get(tenant_id, workspace_id, audit_event_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="recall replay not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "id": str(replay.audit_event.id),
            "tenant_id": str(replay.audit_event.tenant_id),
            "workspace_id": str(workspace_id),
            "created_at": replay.audit_event.created_at.isoformat(),
            "actor": replay.audit_event.actor,
            "actor_type": replay.audit_event.actor_type,
            "operation": replay.operation,
            "query_sha256": replay.query_sha256,
            "query_chars": replay.query_chars,
            "candidate_count": replay.candidate_count,
            "sources_used": list(replay.sources_used),
            "index_stale": replay.index_stale,
            "index_freshness": None
            if replay.index_freshness is None
            else asdict(replay.index_freshness),
            "context_budget_tokens": replay.context_budget_tokens,
            "context_used_tokens": replay.context_used_tokens,
            "trace_ids": [str(item_id) for item_id in replay.trace_ids],
            "references": [
                {
                    "id": str(reference.item_id),
                    "layer": reference.layer,
                    "status": reference.status,
                    "revision": reference.revision,
                }
                for reference in replay.references
            ],
        }

    @app.get("/v1/workspaces/{workspace_id}/seed")
    def get_session_seed(
        workspace_id: UUID,
        tenant_id: UUID = DEFAULT_SERVER_ID,
        budget_tokens: int = 512,
    ) -> dict[str, Any]:
        """Return opt-in bounded shared orientation; use recall for task context."""
        seed = services.session_seed.build(
            tenant_id, workspace_id, budget_tokens=budget_tokens
        )
        return {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id),
            "budget_tokens": seed.budget_tokens,
            "used_tokens": seed.used_tokens,
            "trace_ids": [str(item_id) for item_id in seed.trace_ids],
            "markdown": seed.markdown,
        }

    @app.get("/v1/keys")
    def list_api_keys(tenant_id: UUID = DEFAULT_SERVER_ID) -> dict[str, Any]:
        """List configured API-key metadata without exposing secrets."""
        records = services.api_keys.list_keys(tenant_id)
        return {
            "tenant_id": str(tenant_id),
            "count": len(records),
            "keys": [_api_key_response(record) for record in records],
        }

    @app.post("/v1/identities/provision")
    def provision_identity(
        body: IdentityProvisionBody,
        request: Request,
    ) -> dict[str, Any]:
        """Provision stable foreign-key identities under operator authority."""
        try:
            agent, thread = services.identities.provision(
                ProvisionIdentityCommand(
                    tenant_id=body.tenant_id,
                    workspace_id=body.workspace_id,
                    agent_id=body.agent_id,
                    agent_name=body.agent_name,
                    agent_role=body.agent_role,
                    agent_config=body.agent_config,
                    thread_id=body.thread_id,
                    thread_status=body.thread_status,
                )
            )
        except ValueError as exc:
            status_code = 409 if "already belongs" in str(exc) else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        record_audit(
            request,
            tenant_id=body.tenant_id,
            workspace_id=body.workspace_id,
            action="identity.provision",
            resource_type="agent_identity",
            resource_id=str(body.agent_id),
            metadata={
                "agent_name": agent.name,
                "agent_role": agent.role,
                "thread_id": str(thread.id) if thread else None,
                "thread_status": thread.status if thread else None,
            },
        )
        return {
            "agent": {
                "id": str(agent.id),
                "tenant_id": str(agent.tenant_id),
                "workspace_id": str(agent.workspace_id),
                "name": agent.name,
                "role": agent.role,
                "config": agent.config,
            },
            "thread": None
            if thread is None
            else {
                "id": str(thread.id),
                "tenant_id": str(thread.tenant_id),
                "workspace_id": str(thread.workspace_id),
                "owner_agent_id": str(thread.owner_agent_id)
                if thread.owner_agent_id
                else None,
                "status": thread.status,
            },
        }

    @app.post("/v1/workspaces/provision")
    def provision_workspace(
        body: WorkspaceProvisionBody,
        request: Request,
    ) -> dict[str, Any]:
        """Provision an operator-owned workspace before registering its agents."""
        try:
            workspace = services.identities.provision_workspace(
                ProvisionWorkspaceCommand(
                    tenant_id=body.tenant_id,
                    workspace_id=body.workspace_id,
                    workspace_name=body.workspace_name,
                )
            )
        except ValueError as exc:
            status_code = 409 if "already belongs" in str(exc) else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        record_audit(
            request,
            tenant_id=workspace.tenant_id,
            workspace_id=workspace.id,
            action="workspace.provision",
            resource_type="workspace_identity",
            resource_id=str(workspace.id),
            metadata={"workspace_name": workspace.name},
        )
        return {
            "workspace": {
                "id": str(workspace.id),
                "tenant_id": str(workspace.tenant_id),
                "name": workspace.name,
            }
        }

    @app.post("/v1/keys/{key_id}/revoke")
    def revoke_api_key(
        key_id: UUID,
        body: ApiKeyRevokeBody,
        request: Request,
    ) -> dict[str, Any]:
        """Revoke one key fingerprint while preserving audit evidence."""
        try:
            record = services.api_keys.revoke(
                body.tenant_id,
                key_id,
                reason=body.reason,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="api key not found") from exc
        record_audit(
            request,
            tenant_id=body.tenant_id,
            workspace_id=None,
            action="api_key.revoke",
            resource_type="api_key",
            resource_id=str(key_id),
            metadata={"name": record.name, "reason": body.reason},
        )
        return _api_key_response(record)

    @app.get("/v1/system/status")
    def system_status() -> dict[str, Any]:
        """Return real local process/storage status for the operator UI."""
        load_average: tuple[float, float, float] | None = None
        if hasattr(os, "getloadavg"):
            try:
                raw_load_average = os.getloadavg()
                load_average = (
                    round(raw_load_average[0], 2),
                    round(raw_load_average[1], 2),
                    round(raw_load_average[2], 2),
                )
            except OSError:
                load_average = None
        return {
            "status": "ok",
            "version": app.version,
            "build": build_info.public_dict(),
            "uptime_seconds": round(time.time() - PROCESS_STARTED_AT),
            "storage": _disk_usage_response(),
            "process": {
                "rss_mb": _process_rss_mb(),
                "pid": os.getpid(),
            },
            "load_average": {
                "one_minute": load_average[0] if load_average else None,
                "five_minutes": load_average[1] if load_average else None,
                "fifteen_minutes": load_average[2] if load_average else None,
            },
            "memory_llm": MemoryLLMConfig.from_env().public_dict(),
            "runtime_dependencies": {
                "nats": _private_dependency_health(
                    os.getenv("UAM_NATS_HEALTH_URL", "http://nats:8222/healthz")
                ),
                "embedding_worker": _private_dependency_health(
                    os.getenv(
                        "UAM_EMBEDDING_WORKER_HEALTH_URL",
                        "http://embedding-worker:9091/healthz",
                    )
                ),
            },
        }

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
        embedding_collector = getattr(services.embedding, "collect_metrics", None)
        if callable(embedding_collector):
            rows = {**rows, **embedding_collector()}
        rows = {**rows, **services.retrieval.collect_metrics()}
        worker_snapshot = worker_readiness_snapshot()
        worker_ready_count = (
            sum(state.ready for state in worker_snapshot.required)
            if worker_snapshot is not None
            else 0
        )
        worker_stale_count = (
            len(worker_snapshot.stale_kinds) if worker_snapshot is not None else 0
        )
        worker_missing_count = (
            len(worker_snapshot.missing_kinds)
            if worker_snapshot is not None
            else len(required_workers)
        )
        rows = {
            **rows,
            "worker_required": len(required_workers),
            "worker_ready": worker_ready_count,
            "worker_unready": max(0, len(required_workers) - worker_ready_count),
            "worker_missing": worker_missing_count,
            "worker_stale": worker_stale_count,
        }
        return render_prometheus(rows)

    @app.get("/ui", response_class=HTMLResponse)
    def operator_ui() -> Any:
        """Serve the local human memory console."""
        index = web_dist / "index.html"
        if index.exists():
            return FileResponse(index)
        return HTMLResponse(_OPERATOR_UI_HTML)

    @app.get("/ui/{asset_path:path}")
    def operator_ui_spa(asset_path: str) -> Any:
        """Serve React dashboard files and fall back to SPA index."""
        if not web_dist.exists():
            return HTMLResponse(_OPERATOR_UI_HTML)
        requested = (web_dist / asset_path).resolve()
        try:
            requested.relative_to(web_dist.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="asset not found") from exc
        if requested.is_file():
            return FileResponse(requested)
        index = web_dist / "index.html"
        if index.exists():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="dashboard not built")

    @app.get("/v1/settings/models")
    def get_model_settings() -> dict[str, Any]:
        """Return runtime and desired embedding model settings for the UI."""
        return _model_settings_response(services, model_settings)

    @app.put("/v1/settings/models")
    def save_model_settings(body: ModelSettingsBody, request: Request) -> dict[str, Any]:
        """Save desired model settings without hot-swapping a live vector index."""
        nonlocal model_settings
        previous_settings = model_settings or {}
        proposed_settings = _settings_from_body(body)
        try:
            _assert_model_endpoint_allowed(proposed_settings)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        model_settings = proposed_settings
        if model_settings["api_key"] is None:
            existing_key = previous_settings.get("api_key")
            if existing_key:
                model_settings["api_key"] = existing_key
        try:
            _save_model_settings(model_settings)
        except OSError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        response = _model_settings_response(services, model_settings)
        record_audit(
            request,
            tenant_id=DEFAULT_SERVER_ID,
            workspace_id=DEFAULT_PROJECT_ID,
            action="settings.models.save",
            resource_type="model_settings",
            resource_id=model_settings["model_name"],
            metadata={
                "provider": model_settings["provider"],
                "dimension": model_settings["dimension"],
                "restart_required": response["restart_required"],
            },
        )
        return response

    @app.post("/v1/settings/models/test")
    def test_model_settings(body: ModelSettingsBody) -> dict[str, Any]:
        """Probe an embedding provider using the proposed settings."""
        settings = _settings_from_body(body)
        try:
            _assert_model_endpoint_allowed(settings)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    def retain(body: RetainBody, request: Request) -> dict[str, Any]:
        """Append memory and return its canonical identity and outbox status."""
        principal = _principal_from_request(request)
        audit_event = AuditEvent(
            tenant_id=body.tenant_id,
            workspace_id=body.workspace_id,
            action="memory.retain",
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type="memory_item",
            metadata={"layer": body.layer.value, "status": body.status.value, "source_kind": body.source_kind},
        )
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
                ),
                audit_event=audit_event,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _memory_write_response(result)

    @app.post("/v1/conversations/turns", status_code=201)
    def append_conversation_turn(
        body: ConversationTurnBody, request: Request
    ) -> dict[str, Any]:
        """Append an immutable raw transcript turn separate from curated memory."""
        principal = _principal_from_request(request)
        audit_event = AuditEvent(
            tenant_id=body.tenant_id,
            workspace_id=body.workspace_id,
            action="conversation.turn.append",
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type="conversation_turn",
            metadata={
                "namespace": body.namespace,
                "retention_policy": body.retention_policy.value,
                "message_count": len(body.messages),
            },
        )
        try:
            result = services.conversations.append_turn(
                AppendConversationTurnCommand(
                    tenant_id=body.tenant_id,
                    workspace_id=body.workspace_id,
                    thread_id=body.thread_id,
                    namespace=body.namespace,
                    agent_id=body.agent_id,
                    source_kind=body.source_kind,
                    retention_policy=body.retention_policy,
                    messages=tuple(
                        ConversationMessage(
                            role=message.role,
                            content=message.content,
                            name=message.name,
                            metadata=message.metadata,
                        )
                        for message in body.messages
                    ),
                    metadata=body.metadata,
                    idempotency_key=body.idempotency_key,
                ),
                audit_event=audit_event,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response = _conversation_turn_response(result.turn, created=result.created)
        response["queued_event_ids"] = [
            str(event_id) for event_id in result.queued_event_ids
        ]
        return response

    @app.get("/v1/conversations/turns")
    def list_conversation_turns(
        tenant_id: UUID = DEFAULT_SERVER_ID,
        workspace_id: UUID = DEFAULT_PROJECT_ID,
        thread_id: UUID | None = None,
        namespace: str | None = None,
        limit: int = 50,
        before_created_at: datetime | None = None,
        before_turn_id: UUID | None = None,
    ) -> dict[str, Any]:
        """List recent raw transcript turns for operator review."""
        safe_limit = max(1, min(int(limit), 200))
        turns = services.conversations.list_turns(
            tenant_id,
            workspace_id,
            thread_id=thread_id,
            namespace=namespace,
            before_created_at=before_created_at,
            before_turn_id=before_turn_id,
            limit=safe_limit + 1,
        )
        has_more = len(turns) > safe_limit
        rows = turns[:safe_limit]
        cursor = rows[-1] if has_more and rows else None
        return {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id),
            "count": len(rows),
            "limit": safe_limit,
            "has_more": has_more,
            "next_before_created_at": cursor.created_at.isoformat() if cursor else None,
            "next_before_turn_id": str(cursor.id) if cursor else None,
            "turns": [_conversation_turn_response(turn) for turn in rows],
        }

    @app.post("/v1/workspaces/{workspace_id}/conversations/purge-expired")
    def purge_expired_conversation_turns(
        workspace_id: UUID,
        request: Request,
        tenant_id: UUID = DEFAULT_SERVER_ID,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Purge expired curated-only staging text through an operator maintenance call."""
        turn_ids = services.conversations.purge_expired_turns(
            tenant_id,
            workspace_id,
            limit=limit,
        )
        record_audit(
            request,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            action="conversation.retention.purge_expired",
            resource_type="conversation_turn",
            metadata={"count": len(turn_ids), "limit": max(1, min(int(limit), 5000))},
        )
        return {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id),
            "purged_turn_ids": [str(turn_id) for turn_id in turn_ids],
            "count": len(turn_ids),
        }

    @app.post("/v1/conversations/turns/{turn_id}/curate", status_code=201)
    def curate_conversation_turn(
        turn_id: UUID,
        body: CurateConversationTurnBody,
        request: Request,
    ) -> dict[str, Any]:
        """Distill a raw transcript turn into recallable curated memory."""
        principal = _principal_from_request(request)
        audit_event = AuditEvent(
            tenant_id=body.tenant_id,
            workspace_id=None,
            action="conversation.curate.propose",
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type="memory_proposal",
            metadata={"turn_id": str(turn_id)},
        )
        try:
            result = services.curator.curate_turn(
                CurateConversationTurnCommand(
                    tenant_id=body.tenant_id,
                    turn_id=turn_id,
                    layer=body.layer,
                    kind=body.kind,
                    labels=tuple(body.labels),
                    importance=body.importance,
                    confidence=body.confidence,
                    auto_accept=body.auto_accept,
                    idempotency_key=body.idempotency_key,
                ),
                audit_event=audit_event,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="conversation turn not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if result.proposal is not None:
            proposal = result.proposal.proposal
            return _memory_proposal_response(proposal, created=result.proposal.created)
        assert result.retained is not None
        retained = result.retained
        record_audit(
            request,
            tenant_id=body.tenant_id,
            workspace_id=retained.item.workspace_id,
            action="conversation.curate",
            resource_type="memory_item",
            resource_id=str(retained.item.id),
            metadata={"turn_id": str(turn_id), "created": retained.created},
        )
        return _memory_write_response(retained)

    @app.post("/v1/memory/proposals", status_code=201)
    def submit_memory_proposal(body: MemoryProposalBody, request: Request) -> dict[str, Any]:
        """Store a proposed memory update without directly mutating memory."""
        principal = _principal_from_request(request)
        audit_event = AuditEvent(
            tenant_id=body.tenant_id,
            workspace_id=body.workspace_id,
            action="proposal.submit",
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type="memory_proposal",
            metadata={"namespace": body.namespace, "target": body.target.value},
        )
        try:
            result = services.proposals.submit(
                SubmitMemoryProposalCommand(
                    tenant_id=body.tenant_id,
                    workspace_id=body.workspace_id,
                    namespace=body.namespace,
                    requester=body.requester,
                    target=body.target,
                    proposal=body.proposal,
                    evidence=body.evidence,
                    agent_id=body.agent_id,
                    thread_id=body.thread_id,
                    confidence=body.confidence,
                    importance=body.importance,
                    metadata=body.metadata,
                    idempotency_key=body.idempotency_key,
                ),
                audit_event=audit_event,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _memory_proposal_response(result.proposal, created=result.created)

    @app.get("/v1/memory/proposals")
    def list_memory_proposals(
        tenant_id: UUID = DEFAULT_SERVER_ID,
        workspace_id: UUID = DEFAULT_PROJECT_ID,
        namespace: str | None = None,
        status: MemoryProposalStatus | None = None,
        limit: int = 50,
        before_created_at: datetime | None = None,
        before_proposal_id: UUID | None = None,
    ) -> dict[str, Any]:
        """List proposed memory updates for review."""
        if (before_created_at is None) != (before_proposal_id is None):
            raise HTTPException(
                status_code=422,
                detail="before_created_at and before_proposal_id must be supplied together",
            )
        safe_limit = max(1, min(int(limit), 200))
        proposals = services.proposals.list(
            tenant_id,
            workspace_id,
            namespace=namespace,
            status=status,
            before_created_at=before_created_at,
            before_proposal_id=before_proposal_id,
            limit=safe_limit + 1,
        )
        has_more = len(proposals) > safe_limit
        rows = proposals[:safe_limit]
        cursor = rows[-1] if has_more and rows else None
        return {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id),
            "count": len(rows),
            "limit": safe_limit,
            "has_more": has_more,
            "next_before_created_at": cursor.created_at.isoformat() if cursor else None,
            "next_before_proposal_id": str(cursor.id) if cursor else None,
            "proposals": [_memory_proposal_response(proposal) for proposal in rows],
        }

    @app.post("/v1/memory/proposals/{proposal_id}/accept", status_code=201)
    def accept_memory_proposal(
        proposal_id: UUID,
        body: MemoryProposalReviewBody,
        request: Request,
    ) -> dict[str, Any]:
        """Accept a proposal and create a durable memory item."""
        principal = _principal_from_request(request)
        audit_event = AuditEvent(
            tenant_id=body.tenant_id,
            workspace_id=None,
            action="proposal.accept",
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type="memory_proposal",
            metadata={"reviewer": body.reviewer},
        )
        try:
            result = services.proposals.accept(
                ReviewMemoryProposalCommand(
                    tenant_id=body.tenant_id,
                    proposal_id=proposal_id,
                    reviewer=body.reviewer,
                    reason=body.reason,
                    layer=body.layer,
                    kind=body.kind,
                    labels=tuple(body.labels),
                    idempotency_key=body.idempotency_key,
                ),
                audit_event=audit_event,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="memory proposal not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _memory_proposal_review_response(result)

    @app.post("/v1/memory/proposals/{proposal_id}/reject", status_code=200)
    def reject_memory_proposal(
        proposal_id: UUID,
        body: MemoryProposalReviewBody,
        request: Request,
    ) -> dict[str, Any]:
        """Reject a proposal without creating durable memory."""
        principal = _principal_from_request(request)
        audit_event = AuditEvent(
            tenant_id=body.tenant_id,
            workspace_id=None,
            action="proposal.reject",
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type="memory_proposal",
            metadata={"reviewer": body.reviewer, "reason": body.reason},
        )
        try:
            result = services.proposals.reject(
                ReviewMemoryProposalCommand(
                    tenant_id=body.tenant_id,
                    proposal_id=proposal_id,
                    reviewer=body.reviewer,
                    reason=body.reason,
                ),
                audit_event=audit_event,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="memory proposal not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _memory_proposal_review_response(result)

    @app.put("/v1/memory/{item_id}/supersede", status_code=201)
    def supersede_memory(
        item_id: UUID,
        body: SupersedeMemoryBody,
        request: Request,
    ) -> dict[str, Any]:
        """Append a replacement only when the caller's revision is still current."""
        principal = _principal_from_request(request)
        audit_event = AuditEvent(
            tenant_id=body.tenant_id,
            workspace_id=None,
            action="memory.supersede",
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type="memory_item",
            metadata={
                "supersedes_id": str(item_id),
                "expected_revision": body.expected_revision,
            },
        )
        try:
            result = services.retention.supersede(
                SupersedeMemoryCommand(
                    tenant_id=body.tenant_id,
                    item_id=item_id,
                    replacement_text=body.text,
                    expected_revision=body.expected_revision,
                    confidence=body.confidence,
                    idempotency_key=body.idempotency_key,
                ),
                audit_event=audit_event,
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
    def recall(body: RecallBody, request: Request) -> dict[str, Any]:
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
            per_layer_limit={layer: DEFAULT_CONTEXT_PER_LAYER_LIMIT for layer in MemoryLayer},
        )
        package = services.context.compile(result, recipe)
        principal = _principal_from_request(request)
        audit_event = services.audit.record(
            tenant_id=body.tenant_id,
            workspace_id=body.workspace_id,
            action="memory.recall",
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type="memory_recall",
            metadata={
                # The query itself must never enter the replay/audit record.
                "query_sha256": hashlib.sha256(body.query.encode("utf-8")).hexdigest(),
                "query_chars": len(body.query),
                "operation": body.operation[:256],
                "candidate_count": len(result.candidates),
                "candidate_ids": [str(row.item.id) for row in result.candidates],
                "sources_used": list(result.sources_used),
                "index_stale": result.index_stale,
                "index_freshness": None
                if result.index_freshness is None
                else asdict(result.index_freshness),
                "context_budget_tokens": package.budget_tokens,
                "context_used_tokens": package.used_tokens,
                "trace_ids": [str(item_id) for item_id in package.trace_ids],
            },
        )
        return {
            "replay_id": str(audit_event.id),
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
            "index_stale": result.index_stale,
            "index_freshness": None
            if result.index_freshness is None
            else asdict(result.index_freshness),
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
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List memory rows for local operator review."""
        layers = (layer,) if layer is not None else ()
        lister = getattr(services.store, "list_for_workspace", None)
        if lister is None:
            raise HTTPException(status_code=503, detail="memory listing unavailable")
        safe_limit = max(1, min(limit, 500))
        safe_offset = max(0, offset)
        rows = lister(
            tenant_id,
            workspace_id,
            layers=layers,
            status=status,
            label=label,
            limit=safe_limit + 1,
            offset=safe_offset,
        )
        has_more = len(rows) > safe_limit
        rows = rows[:safe_limit]
        return {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id),
            "count": len(rows),
            "limit": safe_limit,
            "offset": safe_offset,
            "has_more": has_more,
            "next_offset": safe_offset + len(rows) if has_more else None,
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
    def reflect(workspace_id: UUID, tenant_id: UUID, request: Request) -> dict[str, Any]:
        """Run the baseline reflection synchronously behind an async-shaped API."""
        principal = _principal_from_request(request)
        observations = services.reflection.reflect(
            tenant_id,
            workspace_id,
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
        )
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
        request: Request,
    ) -> dict[str, Any]:
        """Persist a human/operator decision for one conflict case."""
        principal = _principal_from_request(request)
        audit_event = AuditEvent(
            tenant_id=body.tenant_id,
            workspace_id=workspace_id,
            action="conflict.decide",
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type="conflict_case",
            resource_id=str(case_id),
            metadata={
                "requested_status": body.status.value,
                "requested_winner_value": body.winner_value,
            },
        )
        try:
            decision = services.conflicts.decide(
                body.tenant_id,
                workspace_id,
                case_id,
                status=body.status,
                winner_value=body.winner_value,
                reason=body.reason,
                audit_event=audit_event,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _conflict_decision_response(decision) or {}

    @app.post("/v1/graph/edges", status_code=201)
    def create_graph_edge(body: GraphEdgeBody, request: Request) -> dict[str, Any]:
        """Create one typed memory graph edge."""
        principal = _principal_from_request(request)
        audit_event = AuditEvent(
            tenant_id=body.tenant_id, workspace_id=body.workspace_id,
            action="graph.edge.create", actor=principal.name,
            actor_type=_audit_actor_type(principal), resource_type="memory_edge",
            metadata={"src_id": str(body.src_id), "dst_id": str(body.dst_id), "edge_type": body.edge_type.value},
        )
        try:
            edge = services.graph.link(
                tenant_id=body.tenant_id,
                workspace_id=body.workspace_id,
                src_id=body.src_id,
                dst_id=body.dst_id,
                edge_type=body.edge_type,
                weight=body.weight,
                provenance_item_id=body.provenance_item_id,
                audit_event=audit_event,
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
        limit: int = 100,
        after_created_at: datetime | None = None,
        after_edge_id: UUID | None = None,
    ) -> dict[str, Any]:
        """List incoming and outgoing graph edges for a memory item."""
        if (after_created_at is None) != (after_edge_id is None):
            raise HTTPException(
                status_code=422,
                detail="after_created_at and after_edge_id must be supplied together",
            )
        safe_limit = max(1, min(int(limit), 500))
        edges = services.graph.neighbors(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            item_id=item_id,
            edge_type=edge_type,
            after_created_at=after_created_at,
            after_edge_id=after_edge_id,
            limit=safe_limit + 1,
        )
        has_more = len(edges) > safe_limit
        rows = edges[:safe_limit]
        cursor = rows[-1] if has_more and rows else None
        return {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id),
            "item_id": str(item_id),
            "count": len(rows),
            "limit": safe_limit,
            "has_more": has_more,
            "next_after_created_at": cursor.created_at.isoformat() if cursor else None,
            "next_after_edge_id": str(cursor.id) if cursor else None,
            "edges": [_graph_edge_response(edge) for edge in rows],
        }

    @app.post("/v1/workspaces/{workspace_id}/reindex", status_code=202)
    def reindex(workspace_id: UUID, tenant_id: UUID, request: Request) -> dict[str, Any]:
        """Re-generate all embeddings for the workspace."""
        principal = _principal_from_request(request)
        intent = services.audit.record(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            action="embedding.reindex.intent",
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type="workspace_vector_index",
            resource_id=str(workspace_id),
            metadata={"model": services.embedding.model_name},
        )
        try:
            count = services.embedding.reindex_all(tenant_id, workspace_id)
        except Exception as exc:
            try:
                services.audit.record(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    action="embedding.reindex",
                    actor=principal.name,
                    actor_type=_audit_actor_type(principal),
                    resource_type="workspace_vector_index",
                    resource_id=str(workspace_id),
                    status="failed",
                    metadata={
                        "intent_event_id": str(intent.id),
                        "error_type": type(exc).__name__,
                    },
                )
            except Exception:
                # The durable intent already proves the operation was started.
                # Do not replace the real reindex failure with an audit outage.
                pass
            raise
        services.audit.record(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            action="embedding.reindex",
            actor=principal.name,
            actor_type=_audit_actor_type(principal),
            resource_type="workspace_vector_index",
            resource_id=str(workspace_id),
            metadata={
                "intent_event_id": str(intent.id),
                "reindexed_count": count,
                "model": services.embedding.model_name,
            },
        )
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
                    "editable_content": editable_vault_content(file.content),
                }
                for file in vault.files
            ],
        }

    @app.get("/v1/workspaces/{workspace_id}/vault/health")
    def vault_health(
        workspace_id: UUID,
        tenant_id: UUID = DEFAULT_SERVER_ID,
    ) -> dict[str, Any]:
        """Inspect vault projection integrity without invoking models or mutating memory."""
        report = services.vault_health.inspect(tenant_id, workspace_id)
        return {
            "tenant_id": str(report.tenant_id),
            "workspace_id": str(report.workspace_id),
            "healthy": report.healthy,
            "memory_count": report.memory_count,
            "edge_count": report.edge_count,
            "observation_count": report.observation_count,
            "recallable_head_count": report.recallable_head_count,
            "unlinked_head_count": report.unlinked_head_count,
            "error_count": report.error_count,
            "warning_count": report.warning_count,
            "issues": [
                {
                    "severity": issue.severity,
                    "code": issue.code,
                    "message": issue.message,
                    "item_id": str(issue.item_id) if issue.item_id else None,
                    "edge_id": str(issue.edge_id) if issue.edge_id else None,
                    "observation_id": str(issue.observation_id)
                    if issue.observation_id
                    else None,
                }
                for issue in report.issues
            ],
        }

    @app.post("/v1/workspaces/{workspace_id}/vault/import")
    def import_vault(
        workspace_id: UUID,
        body: VaultImportBody,
        request: Request,
    ) -> dict[str, Any]:
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
        record_audit(
            request,
            tenant_id=body.tenant_id,
            workspace_id=workspace_id,
            action="vault.import.plan" if body.dry_run else "vault.import.apply",
            resource_type="vault",
            metadata={
                "dry_run": result.dry_run,
                "file_count": len(body.files),
                "supersede_count": result.supersede_count,
            },
        )
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

    @app.post("/v1/workspaces/{workspace_id}/vault/archive")
    def archive_vault_file(
        workspace_id: UUID,
        body: VaultDeleteBody,
        request: Request,
    ) -> dict[str, Any]:
        """Archive one memory note without physically deleting audit history."""
        try:
            result = services.vault.archive_file(
                body.tenant_id,
                workspace_id,
                VaultImportSource(path=body.file.path, content=body.file.content),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        record_audit(
            request,
            tenant_id=body.tenant_id,
            workspace_id=workspace_id,
            action="vault.archive",
            resource_type="vault_file",
            resource_id=body.file.path,
            metadata={"change_count": len(result.changes)},
        )
        return {
            "tenant_id": str(result.tenant_id),
            "workspace_id": str(result.workspace_id),
            "dry_run": result.dry_run,
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

    @app.patch("/v1/workspaces/{workspace_id}/vault/memories/{item_id}")
    def patch_vault_memory(
        workspace_id: UUID,
        item_id: UUID,
        body: VaultPatchBody,
        request: Request,
    ) -> dict[str, Any]:
        """Patch text through CAS supersede; never expose or accept vector payloads."""
        section = body.replace_section
        try:
            result = services.vault.patch_memory(
                VaultPatchCommand(
                    tenant_id=body.tenant_id,
                    workspace_id=workspace_id,
                    item_id=item_id,
                    expected_revision=body.expected_revision,
                    replace_body=body.replace_body,
                    section_heading=section.heading if section else None,
                    section_content=section.content if section else None,
                    confidence=body.confidence,
                    idempotency_key=body.idempotency_key,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="memory item not found") from exc
        except MemoryRevisionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        record_audit(
            request,
            tenant_id=body.tenant_id,
            workspace_id=workspace_id,
            action="vault.memory.patch",
            resource_type="memory_item",
            resource_id=str(result.item.id),
            metadata={
                "parent_item_id": str(item_id),
                "expected_revision": body.expected_revision,
                "revision": result.item.revision,
                "changed": result.changed,
                "patch_kind": "section" if section else "body",
                "reindex_queued": bool(result.queued_event_ids),
            },
        )
        return {
            "tenant_id": str(body.tenant_id),
            "workspace_id": str(workspace_id),
            "item_id": str(result.item.id),
            "supersedes_id": str(result.item.supersedes_id)
            if result.item.supersedes_id
            else None,
            "revision": result.item.revision,
            "changed": result.changed,
            "reindex_queued": bool(result.queued_event_ids),
            "queued_event_ids": [str(event_id) for event_id in result.queued_event_ids],
        }

    # ── Checkpoint endpoints ────────────────────────────────────────

    @app.post("/v1/checkpoints", status_code=201)
    def save_checkpoint(body: CheckpointSaveBody, request: Request) -> dict[str, Any]:
        """Save a new working-memory checkpoint revision."""
        principal = _principal_from_request(request)
        try:
            cp = services.checkpoint.save(
                tenant_id=body.tenant_id,
                workspace_id=body.workspace_id,
                thread_id=body.thread_id,
                state=body.state,
                audit_event=AuditEvent(
                    tenant_id=body.tenant_id,
                    workspace_id=body.workspace_id,
                    action="checkpoint.save",
                    actor=principal.name,
                    actor_type=_audit_actor_type(principal),
                    resource_type="checkpoint",
                    metadata={"thread_id": str(body.thread_id)},
                ),
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

    @app.get("/v1/checkpoints/page")
    def list_checkpoints_page(
        workspace_id: UUID = DEFAULT_PROJECT_ID,
        tenant_id: UUID = DEFAULT_SERVER_ID,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Page checkpoint heads without breaking the legacy list endpoint."""
        safe_limit, safe_offset = max(1, min(limit, 500)), max(0, offset)
        heads = services.checkpoint.list_for_workspace(
            tenant_id, workspace_id, limit=safe_limit + 1, offset=safe_offset
        )
        has_more = len(heads) > safe_limit
        rows = heads[:safe_limit]
        return {
            "checkpoints": [_checkpoint_response(cp) for cp in rows],
            "limit": safe_limit,
            "offset": safe_offset,
            "has_more": has_more,
            "next_offset": safe_offset + len(rows) if has_more else None,
        }

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
        thread_id: UUID, body: CheckpointUpdateBody, request: Request
    ) -> dict[str, Any]:
        """CAS-update a checkpoint; returns 409 on stale revision."""
        principal = _principal_from_request(request)
        try:
            cp = services.checkpoint.update(
                tenant_id=body.tenant_id,
                workspace_id=body.workspace_id,
                thread_id=thread_id,
                state=body.state,
                expected_revision=body.expected_revision,
                audit_event=AuditEvent(
                    tenant_id=body.tenant_id,
                    workspace_id=body.workspace_id,
                    action="checkpoint.update",
                    actor=principal.name,
                    actor_type=_audit_actor_type(principal),
                    resource_type="checkpoint",
                    metadata={
                        "thread_id": str(thread_id),
                        "expected_revision": body.expected_revision,
                    },
                ),
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
        thread_id: UUID, body: CheckpointCompactBody, request: Request
    ) -> dict[str, Any]:
        """Delete old revisions keeping the most recent *keep_last*."""
        principal = _principal_from_request(request)
        deleted = services.checkpoint.compact(
            tenant_id=body.tenant_id,
            thread_id=thread_id,
            keep_last=body.keep_last,
            audit_event=AuditEvent(
                tenant_id=body.tenant_id,
                workspace_id=body.workspace_id,
                action="checkpoint.compact",
                actor=principal.name,
                actor_type=_audit_actor_type(principal),
                resource_type="checkpoint_thread",
                resource_id=str(thread_id),
                metadata={"keep_last": body.keep_last},
            ),
        )
        return {"deleted": deleted}

    return app


def _build_runtime_container() -> Container:
    """Select durable Docker mode when a database connection is configured."""
    dsn = read_database_dsn()
    if not dsn:
        return build_in_memory_container()
    server_id = UUID(os.getenv("UAM_SERVER_ID", str(DEFAULT_SERVER_ID)))
    project_id = UUID(os.getenv("UAM_PROJECT_ID", str(DEFAULT_PROJECT_ID)))
    qdrant_url = os.getenv("UAM_QDRANT_URL")
    qdrant_collection = os.getenv("UAM_QDRANT_COLLECTION", "memory_items")
    qdrant_dim = int(os.getenv("UAM_EMBEDDING_DIM", "1536"))
    return build_postgres_container(
        dsn,
        server_id=server_id,
        project_id=project_id,
        qdrant_url=qdrant_url,
        qdrant_dim=qdrant_dim,
        qdrant_collection=qdrant_collection,
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
    html {
      overflow-x: hidden;
      background: #030712;
    }
    body {
      margin: 0;
      min-height: 100vh;
      overflow-x: hidden;
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
    /* Operator dashboard visual system. */
    body {
      background:
        radial-gradient(circle at 70% 2%, rgba(37, 99, 235, .20), transparent 23rem),
        radial-gradient(circle at 92% 8%, rgba(124, 58, 237, .23), transparent 26rem),
        linear-gradient(135deg, #030712 0%, #06101f 42%, #020617 100%);
    }
    body::after {
      content: "";
      position: fixed;
      top: 0;
      left: 210px;
      right: 0;
      height: 250px;
      pointer-events: none;
      opacity: .84;
      background:
        radial-gradient(ellipse at 78% 0%, rgba(124, 58, 237, .42), transparent 24rem),
        radial-gradient(ellipse at 64% 34%, rgba(37, 99, 235, .45), transparent 18rem),
        repeating-radial-gradient(ellipse at 76% 18%, rgba(59, 130, 246, .22) 0 1px, transparent 1px 9px);
      filter: blur(.2px);
      mask-image: linear-gradient(to bottom, #000, transparent 92%);
    }
    .reference-sidebar {
      position: fixed;
      z-index: 10;
      inset: 0 auto 0 0;
      width: 210px;
      padding: 28px 12px 18px;
      border-right: 1px solid rgba(96, 165, 250, .18);
      background:
        linear-gradient(180deg, rgba(2, 10, 24, .96), rgba(3, 7, 18, .96)),
        radial-gradient(circle at 28% 8%, rgba(14, 165, 233, .20), transparent 8rem);
      box-shadow: 20px 0 60px rgba(0, 0, 0, .28);
      overflow: hidden;
    }
    .side-logo {
      width: 42px;
      height: 42px;
      margin: 0 0 34px 14px;
      border-radius: 999px;
      background:
        radial-gradient(circle at 50% 50%, rgba(34, 211, 238, .24), transparent 42%),
        conic-gradient(from 40deg, #38bdf8, #2563eb, #8b5cf6, #38bdf8);
      box-shadow: 0 0 28px rgba(56, 189, 248, .42), inset 0 0 0 8px rgba(2, 6, 23, .76);
    }
    .reference-sidebar .nav-title {
      margin: 20px 14px 10px;
      color: #a7b6d8;
      font-size: 11px;
      letter-spacing: .09em;
    }
    .reference-sidebar .nav-button {
      min-height: 40px;
      margin-bottom: 8px;
      border-radius: 8px;
      justify-content: flex-start;
      padding: 10px 12px;
      color: #c7d2fe;
      background: transparent;
      border-color: transparent;
      box-shadow: none;
    }
    .reference-sidebar .nav-button.primary,
    .reference-sidebar .nav-button:hover {
      background: linear-gradient(90deg, rgba(37, 99, 235, .58), rgba(59, 130, 246, .16));
      border-color: rgba(96, 165, 250, .32);
      color: #f8fbff;
    }
    .health-card {
      position: absolute;
      left: 12px;
      right: 12px;
      bottom: 118px;
      padding: 14px;
      border: 1px solid rgba(96, 165, 250, .17);
      border-radius: 14px;
      background: rgba(2, 13, 29, .72);
    }
    .mini-meter {
      height: 5px;
      overflow: hidden;
      border-radius: 999px;
      background: rgba(30, 41, 59, .9);
      margin: 8px 0 14px;
    }
    .mini-meter span {
      display: block;
      width: 68%;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #3b82f6, #8b5cf6);
    }
    .shell {
      position: relative;
      z-index: 1;
      width: auto;
      max-width: none;
      margin: 0 clamp(14px, 1.6vw, 28px) 0 222px;
      padding: clamp(18px, 2vw, 30px) 0 10px;
      overflow-x: hidden;
    }
    header.hero {
      min-height: 116px;
      margin-bottom: 14px;
      padding: 18px 22px 6px;
      border: 0;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
      overflow: visible;
    }
    header.hero::after {
      content: "";
      position: absolute;
      z-index: -1;
      top: 38px;
      right: 4%;
      width: 560px;
      height: 132px;
      opacity: .9;
      background:
        radial-gradient(ellipse at 40% 55%, rgba(59, 130, 246, .65), transparent 5rem),
        radial-gradient(ellipse at 70% 40%, rgba(139, 92, 246, .55), transparent 7rem);
      filter: blur(28px);
    }
    header.hero .brand { display: none; }
    h1 {
      margin-top: 20px;
      font-size: clamp(34px, 4vw, 44px);
      letter-spacing: -.045em;
      text-shadow: 0 0 34px rgba(96, 165, 250, .22);
    }
    .lede { max-width: 630px; font-size: 14px; color: #b8c3da; }
    .hero-actions { align-self: start; padding-top: 34px; }
    .hero-actions button:first-child { display: none; }
    .hero-actions button.secondary {
      min-height: 34px;
      border-radius: 999px;
      padding: 8px 14px;
      color: #dbeafe;
      background: rgba(2, 6, 23, .55);
    }
    .hero-actions button.secondary::before {
      content: "";
      width: 7px;
      height: 7px;
      margin-right: 8px;
      border-radius: 999px;
      display: inline-block;
      background: #22c55e;
      box-shadow: 0 0 12px rgba(34, 197, 94, .9);
    }
    .identity-kpi {
      margin-bottom: 14px;
      border: 0;
      background: transparent;
      box-shadow: none;
      backdrop-filter: none;
    }
    .identity-kpi .panel-body { padding: 0 12px; }
    .identity-kpi .form-grid {
      position: absolute;
      left: -9999px;
      width: 1px;
      height: 1px;
      overflow: hidden;
    }
    .kpis {
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin: 0;
    }
    .kpi {
      min-height: 108px;
      padding: 20px 20px 16px 80px;
      border-radius: 12px;
      border-color: rgba(96, 165, 250, .24);
      background:
        linear-gradient(135deg, rgba(15, 30, 55, .90), rgba(3, 11, 25, .72)),
        radial-gradient(circle at 10% 20%, rgba(37, 99, 235, .22), transparent 7rem);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .04);
      overflow: hidden;
    }
    .kpi::before {
      content: "▣";
      position: absolute;
      left: 18px;
      top: 23px;
      width: 42px;
      height: 42px;
      display: grid;
      place-items: center;
      border-radius: 10px;
      color: #60a5fa;
      background: rgba(37, 99, 235, .20);
      font-size: 24px;
    }
    .kpi:nth-child(2)::before { content: "⚖"; color: #c084fc; background: rgba(126, 34, 206, .22); }
    .kpi:nth-child(3)::before { content: "▰"; color: #67e8f9; background: rgba(8, 145, 178, .20); }
    .kpi:nth-child(4)::before { content: "⌁"; color: #6ee7b7; background: rgba(5, 150, 105, .18); }
    .kpi .value { font-size: 25px; line-height: 1.15; }
    .kpi .label { margin-bottom: 4px; color: #b6c2d9; letter-spacing: .08em; }
    .kpi .sub {
      margin-top: 8px;
      color: #69f0ae;
      font-size: 12px;
    }
    .kpi .mini-chart {
      position: absolute;
      right: 18px;
      bottom: 20px;
      width: 86px;
      height: 38px;
      opacity: .95;
    }
    .kpi .mini-chart path {
      fill: none;
      stroke: #60a5fa;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
      filter: drop-shadow(0 0 8px rgba(96, 165, 250, .45));
    }
    .kpi:nth-child(2) .mini-chart path { stroke: #a855f7; }
    .kpi:nth-child(3) .mini-chart path { stroke: #22d3ee; }
    .kpi:nth-child(4) .mini-chart path { stroke: #4ade80; }
    .cockpit {
      display: none;
    }
    .grid {
      grid-template-columns:
        minmax(340px, .95fr)
        minmax(360px, .92fr)
        minmax(270px, .58fr);
      grid-template-areas:
        "memory graph ops"
        "vault conflicts ops";
      gap: 12px;
      padding: 0 12px 20px;
      align-items: stretch;
      min-width: 0;
    }
    .grid > section.panel:first-child {
      grid-area: memory;
      height: 496px;
      min-height: 496px;
      overflow: hidden;
    }
    .grid > section.panel:first-child .panel-body {
      max-height: 438px;
      overflow: auto;
    }
    .grid > aside.panel { grid-area: ops; }
    .dashboard-graph-panel { grid-area: graph; height: 496px; }
    .dashboard-vault-panel { grid-area: vault; height: 210px; }
    .dashboard-conflict-panel { grid-area: conflicts; height: 210px; }
    .grid > aside.panel {
      height: 718px;
      overflow: hidden;
    }
    .grid > aside.panel .panel-body {
      max-height: 660px;
      overflow: auto;
    }
    .panel {
      border-radius: 12px;
      border-color: rgba(96, 165, 250, .22);
      background: rgba(3, 12, 26, .78);
      box-shadow: none;
      min-width: 0;
    }
    .panel-head { padding: 16px 16px 0; }
    .panel-body { padding: 14px 16px 16px; }
    .tabs {
      padding: 0 10px;
      min-height: 45px;
      align-items: stretch;
      background: rgba(3, 12, 26, .72);
    }
    .tab {
      min-width: 84px;
      min-height: 44px;
      border-radius: 0;
      font-weight: 650;
    }
    .tab.active {
      color: #60a5fa;
      background: transparent;
      border: 0;
      border-bottom: 2px solid #3b82f6;
    }
    #view-memory .panel-head h2::after { content: "Recent Memories"; font-size: 0; }
    #view-memory .form-grid {
      grid-template-columns: 1fr auto auto;
      align-items: center;
    }
    #query, #layer, #status, #label, #view-memory .form-grid button {
      min-height: 36px;
      padding: 8px 10px;
      border-radius: 8px;
      font-size: 12px;
    }
    #label { display: none; }
    .card {
      border-radius: 8px;
      background: rgba(10, 22, 40, .76);
      border-color: rgba(51, 65, 85, .82);
      padding: 13px 14px;
      min-width: 0;
    }
    .card::before { width: 0; }
    .memory-text { font-size: 13px; color: #dbeafe; }
    .memory-text + .row { margin-top: 8px !important; }
    .pill { border-radius: 6px; padding: 3px 8px; font-size: 11px; }
    .force-graph {
      min-height: 390px;
      border-radius: 10px;
      border-color: transparent;
      background:
        radial-gradient(circle at 50% 46%, rgba(29, 78, 216, .32), transparent 9rem),
        rgba(2, 8, 20, .28);
    }
    #referenceGraph {
      min-height: 360px;
      height: 360px;
      touch-action: auto;
    }
    #referenceGraph .force-svg {
      cursor: default;
    }
    .graph-tools {
      right: 14px;
      bottom: 14px;
      border-radius: 8px;
      transform: scale(.86);
      transform-origin: bottom right;
    }
    .log { max-height: 430px; }
    .ops-action {
      display: grid;
      grid-template-columns: 42px 1fr auto;
      gap: 12px;
      align-items: center;
      padding: 12px;
      border: 1px solid rgba(96, 165, 250, .22);
      border-radius: 10px;
      background: linear-gradient(135deg, rgba(59, 130, 246, .12), rgba(15, 23, 42, .42));
      margin-bottom: 10px;
    }
    .ops-icon {
      width: 38px;
      height: 38px;
      display: grid;
      place-items: center;
      border-radius: 10px;
      background: rgba(59, 130, 246, .17);
      color: #93c5fd;
      font-size: 19px;
    }
    .activity-item {
      display: grid;
      grid-template-columns: 30px 1fr auto;
      gap: 10px;
      align-items: start;
      padding: 10px;
      border: 1px solid rgba(51, 65, 85, .54);
      border-radius: 8px;
      background: rgba(15, 23, 42, .40);
      margin-bottom: 7px;
      min-width: 0;
    }
    .activity-item strong,
    .activity-item .muted,
    .ops-action strong,
    .ops-action .muted {
      overflow-wrap: anywhere;
    }
    .activity-dot {
      width: 24px;
      height: 24px;
      display: grid;
      place-items: center;
      border-radius: 7px;
      background: rgba(37, 99, 235, .16);
      color: #60a5fa;
      font-size: 12px;
    }
    .dashboard-vault-panel pre { max-height: 158px; font-size: 11px; }
    .dashboard-vault-tree {
      display: grid;
      grid-template-columns: 160px 1fr;
      gap: 10px;
    }
    .tree-list {
      padding: 8px;
      border: 1px solid rgba(51, 65, 85, .72);
      border-radius: 8px;
      background: rgba(2, 8, 20, .46);
      font-size: 12px;
      line-height: 1.9;
    }
    @media (max-width: 1360px) {
      .reference-sidebar {
        width: 178px;
        padding-inline: 10px;
      }
      .reference-sidebar .nav-button {
        font-size: 12px;
        padding-inline: 10px;
      }
      .health-card {
        padding: 12px 10px;
        font-size: 12px;
      }
      body::after { left: 178px; }
      .shell { margin-left: 190px; margin-right: 12px; }
      .grid {
        grid-template-columns: minmax(330px, 1fr) minmax(330px, 1fr);
        grid-template-areas:
          "memory graph"
          "vault conflicts"
          "ops ops";
      }
      .grid > aside.panel { height: auto; }
      .grid > aside.panel .panel-body { max-height: none; }
      .kpi { padding-left: 68px; }
      .kpi::before { width: 36px; height: 36px; font-size: 20px; }
      .kpi .mini-chart { width: 70px; }
    }
    @media (max-width: 1220px) {
      .reference-sidebar {
        width: 74px;
        padding-inline: 8px;
      }
      .reference-sidebar .nav-title,
      .reference-sidebar .nav-button span,
      .reference-sidebar .health-card {
        display: none;
      }
      .reference-sidebar .nav-button {
        justify-content: center;
        padding-inline: 6px;
        font-size: 0;
      }
      .reference-sidebar .nav-button::first-letter {
        font-size: 17px;
      }
      .side-logo {
        margin-left: auto;
        margin-right: auto;
      }
      body::after { left: 74px; }
      .shell { margin-left: 86px; }
      .grid {
        grid-template-columns: minmax(0, 1fr);
        grid-template-areas:
          "memory"
          "graph"
          "vault"
          "conflicts"
          "ops";
      }
      .grid > section.panel:first-child,
      .dashboard-graph-panel,
      .dashboard-vault-panel,
      .dashboard-conflict-panel,
      .grid > aside.panel {
        height: auto;
        min-height: 0;
      }
    }
    @media (max-width: 1100px) {
      .reference-sidebar { display: none; }
      body::after { left: 0; }
      .shell { width: min(100vw - 24px, 1500px); margin: 0 auto; overflow-x: visible; }
      header.hero, .grid, .split, .cockpit-layout { grid-template-columns: 1fr; }
      header.hero {
        min-height: auto;
        padding-top: 16px;
      }
      header.hero::after {
        right: -12%;
        width: 420px;
        height: 110px;
      }
      .grid {
        display: flex;
        flex-direction: column;
        grid-template-areas: none;
      }
      .grid > section.panel, .grid > aside.panel, .dashboard-graph-panel, .dashboard-vault-panel, .dashboard-conflict-panel { grid-area: auto; }
      .grid > section.panel:first-child, .dashboard-graph-panel, .dashboard-vault-panel, .dashboard-conflict-panel {
        height: auto;
        min-height: 0;
      }
      .grid > section.panel:first-child .panel-body { max-height: 520px; }
      .hero-actions { justify-content: flex-start; }
      .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .kpi { min-height: 100px; }
      .form-grid { grid-template-columns: 1fr 1fr; }
      #referenceGraph { min-height: 340px; height: 340px; }
    }
    @media (max-width: 680px) {
      .shell { width: min(100vw - 20px, 1500px); padding: 10px 0; }
      header.hero { min-height: auto; border-radius: 20px; padding: 18px 4px; grid-template-columns: 1fr; }
      h1 { font-size: 32px; }
      .kpis, .form-grid { grid-template-columns: 1fr; }
      .form-grid .wide { grid-column: auto; }
      .kpi { min-height: 96px; padding-right: 116px; }
      .tabs { overflow-x: auto; flex-wrap: nowrap; }
      .tab { min-width: 92px; }
      .dashboard-vault-tree { grid-template-columns: 1fr; }
      #referenceGraph { min-height: 300px; height: 300px; }
      .graph-tools { position: static; margin-top: 10px; transform: none; }
    }
  </style>
</head>
<body>
  <aside class="reference-sidebar" aria-label="Главная навигация">
    <div class="side-logo" aria-hidden="true"></div>
    <div class="nav-title">Обзор</div>
    <button class="nav-button primary" onclick="showTab('memory')">▦ Панель</button>
    <button class="nav-button" onclick="showTab('conflicts')">✉ Входящие <span class="pill warn" style="margin-left:auto">3</span></button>
    <button class="nav-button" onclick="refreshAll()">⌁ Активность</button>
    <button class="nav-button" onclick="showTab('settings')">⚙ Настройки</button>
    <div class="nav-title">Система</div>
    <button class="nav-button" onclick="showTab('memory')">♙ Агенты</button>
    <button class="nav-button" onclick="showTab('memory')">▤ Источники памяти</button>
    <button class="nav-button" onclick="showTab('settings')">⌬ Интеграции</button>
    <button class="nav-button" onclick="showTab('vault')">▣ Файлы</button>
    <button class="nav-button" onclick="showTab('settings')">♧ Пользователи</button>
    <div class="health-card">
      <div class="agent-row"><strong>Состояние</strong><span class="pill ok">Live</span></div>
      <div class="muted tiny" style="margin-top:10px">Версия <span id="fallbackVersion" style="float:right">загрузка…</span></div>
      <div class="muted tiny">Uptime <span id="fallbackUptime" style="float:right">загрузка…</span></div>
      <div class="muted tiny">Диск сервера <span id="fallbackStorage" style="float:right">загрузка…</span></div>
      <div class="mini-meter"><span></span></div>
      <div class="muted tiny">Load 1m <span id="fallbackLoad" style="float:right">загрузка…</span></div>
      <div class="sparkline" style="width:88%;height:3px;margin:8px 0 12px"></div>
      <div class="muted tiny">RSS процесса <span id="fallbackRss" style="float:right">загрузка…</span></div>
      <div class="sparkline" style="width:72%;height:3px;margin-top:8px"></div>
    </div>
  </aside>
  <div class="shell">
    <header class="hero">
      <div>
        <div class="brand"><span class="orb"></span> Self-hosted</div>
        <h1>Obelisk Memory</h1>
        <p class="lede">
          Единый слой долговременной памяти для OpenClaw, Hermes и других агентов.
        </p>
      </div>
      <div class="hero-actions">
        <button onclick="refreshAll()">Обновить пульт</button>
        <button class="secondary" onclick="showTab('settings')">ЛОКАЛЬНО</button>
      </div>
    </header>

    <section class="panel identity-kpi">
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
          <div class="kpi">
            <div class="label">Memories</div>
            <div id="kpiMemories" class="value">—</div>
            <div class="sub">+ live workspace</div>
            <svg class="mini-chart" viewBox="0 0 90 40"><path d="M2 34 L16 28 L28 31 L42 19 L54 23 L66 12 L78 15 L88 6"></path></svg>
          </div>
          <div class="kpi">
            <div class="label">Conflicts</div>
            <div id="kpiConflicts" class="value">—</div>
            <div class="sub" style="color:#c084fc">need review</div>
            <svg class="mini-chart" viewBox="0 0 90 40"><path d="M2 35 L16 32 L28 27 L42 18 L54 22 L66 12 L78 16 L88 7"></path></svg>
          </div>
          <div class="kpi">
            <div class="label">Vault files</div>
            <div id="kpiVault" class="value">—</div>
            <div class="sub" style="color:#22d3ee">human editable</div>
            <svg class="mini-chart" viewBox="0 0 90 40"><path d="M2 36 L14 34 L26 25 L38 28 L50 17 L62 20 L74 10 L88 5"></path></svg>
          </div>
          <div class="kpi">
            <div class="label">Live status</div>
            <div id="kpiStatus" class="value">Активно</div>
            <div class="sub">All systems operational</div>
            <svg class="mini-chart" viewBox="0 0 90 40"><path d="M2 31 L14 29 L26 33 L38 21 L50 25 L62 17 L74 19 L88 5"></path></svg>
          </div>
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
                <span class="muted tiny">Провайдер</span>
                <select id="modelProvider" aria-label="Embedding provider">
                  <option value="fake">fake / тестовый</option>
                  <option value="openai-compatible">OpenAI-compatible gateway</option>
                  <option value="tei">TEI / Jina / vLLM gateway</option>
                  <option value="ollama">Ollama</option>
                  <option value="openai">OpenAI-hosted profile</option>
                </select>
              </label>
              <label>
                <span class="muted tiny">Модель</span>
                <input id="modelName" aria-label="Embedding model" placeholder="text-embedding-3-large">
              </label>
              <label>
                <span class="muted tiny">Размерность</span>
                <input id="modelDim" aria-label="Embedding dimension" type="number" min="1" max="65536" value="3072">
              </label>
              <label>
                <span class="muted tiny">Таймаут, сек</span>
                <input id="modelTimeout" aria-label="Embedding timeout" type="number" min="1" max="600" value="30">
              </label>
              <label class="full">
                <span class="muted tiny">Base URL</span>
                <input id="modelBaseUrl" aria-label="Embedding base URL" placeholder="https://api.openai.com/v1">
              </label>
              <label class="full">
                <span class="muted tiny">API key, если нужен</span>
                <input id="modelApiKey" aria-label="Embedding API key" placeholder="оставь пустым, если endpoint локальный">
              </label>
            </div>
            <div class="toolbar" style="margin-top:12px">
              <button onclick="saveModelSettings()">Сохранить конфиг модели</button>
              <button class="secondary" onclick="testModelSettings()">Проверить endpoint</button>
              <button class="secondary" onclick="reindex()">Переиндексация после применения</button>
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

      <section class="panel dashboard-graph-panel">
        <div class="panel-head">
          <div>
            <h2>Граф памяти <span class="muted tiny">ⓘ</span></h2>
          </div>
          <button class="secondary" onclick="showTab('graph')">↗ Развернуть</button>
        </div>
        <div class="panel-body">
          <div id="referenceGraph" class="force-graph" aria-label="Reference memory graph">
            <svg class="force-svg" role="img"></svg>
          </div>
          <div class="legend">
            <span class="pill ok">Ядро памяти</span>
            <span class="pill warn">Семантическая память</span>
            <span class="pill">Контекстная память</span>
            <span class="pill">Слабая связь</span>
          </div>
        </div>
      </section>

      <section class="panel dashboard-vault-panel">
        <div class="panel-head">
          <div><h2>Предпросмотр файлов</h2></div>
          <button class="secondary" onclick="showTab('vault')">↗</button>
        </div>
        <div class="panel-body dashboard-vault-tree">
          <div class="tree-list">
            ▸ 📁 00_Inbox <span class="pill">12</span><br>
            ▾ 📁 01_Projects<br>
            &nbsp;&nbsp;› 📄 ai-memory<br>
            &nbsp;&nbsp;› 📁 research<br>
            &nbsp;&nbsp;› 📁 personal<br>
            ▸ 📁 02_Resources<br>
            ▸ 📁 03_Archives
          </div>
          <pre># AI Memory System
## Goal
Build a universal memory layer for AI agents that is:
- Self-hosted
- Agent-agnostic
- Privacy-first
- Extensible

## Core Features</pre>
        </div>
      </section>

      <section class="panel dashboard-conflict-panel">
        <div class="panel-head">
          <div><h2>Conflict Review Inbox <span class="pill warn">3</span></h2></div>
          <button class="ghost" onclick="showTab('conflicts')">View all</button>
        </div>
        <div class="panel-body">
          <div class="activity-item"><span class="activity-dot">▣</span><div><strong>Timezone preference mismatch</strong><div class="muted tiny">UTC+2 vs UTC+3</div></div><span class="pill warn">medium</span></div>
          <div class="activity-item"><span class="activity-dot">↔</span><div><strong>Response length preference</strong><div class="muted tiny">Concise vs Detailed</div></div><span class="pill ok">low</span></div>
          <div class="activity-item"><span class="activity-dot">⌘</span><div><strong>Tool preference conflict</strong><div class="muted tiny">Obsidian vs Notion</div></div><span class="pill ok">low</span></div>
          <button style="width:100%;min-height:34px;margin-top:4px" onclick="showTab('conflicts')">Review All Conflicts →</button>
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
          <div>
            <button class="ops-action" onclick="reflect()"><span class="ops-icon">✣</span><span><strong>Рефлексия</strong><br><span class="muted tiny">Синтезировать наблюдения</span></span></button>
            <button class="ops-action" onclick="reindex()"><span class="ops-icon">⟳</span><span><strong>Переиндексация</strong><br><span class="muted tiny">Обновить векторы</span></span></button>
            <button class="ops-action" onclick="loadConflicts()"><span class="ops-icon">✉</span><span><strong>Входящие</strong><br><span class="muted tiny">Разобрать конфликты</span></span><span class="pill warn">3</span></button>
          </div>
          <h3 style="margin-top:18px">Журнал активности <button class="ghost" style="float:right;min-height:20px;padding:0">Все</button></h3>
          <div class="activity-item"><span class="activity-dot">✧</span><div><strong>New memory ingested</strong><div class="muted tiny">User prefers concise answers</div></div><span class="muted tiny">2m</span></div>
          <div class="activity-item"><span class="activity-dot" style="color:#fb7185">!</span><div><strong style="color:#fb7185">Conflict detected</strong><div class="muted tiny">Timezone preference mismatch</div></div><span class="muted tiny">18m</span></div>
          <div class="activity-item"><span class="activity-dot">↻</span><div><strong>Memory reindexed</strong><div class="muted tiny">AI Memory System Project</div></div><span class="muted tiny">42m</span></div>
          <div class="activity-item"><span class="activity-dot">▣</span><div><strong>Vault file added</strong><div class="muted tiny">/Projects/ai-memory/roadmap.md</div></div><span class="muted tiny">1h</span></div>
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

    function formatBytes(value) {
      if (value == null) return "н/д";
      const units = ["B", "KiB", "MiB", "GiB", "TiB"];
      let size = Number(value);
      let unit = 0;
      while (size >= 1024 && unit < units.length - 1) {
        size /= 1024;
        unit += 1;
      }
      return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${units[unit]}`;
    }

    function formatDuration(seconds) {
      if (seconds == null) return "н/д";
      const days = Math.floor(seconds / 86400);
      const hours = Math.floor((seconds % 86400) / 3600);
      const minutes = Math.floor((seconds % 3600) / 60);
      if (days > 0) return `${days}д ${hours}ч`;
      if (hours > 0) return `${hours}ч ${minutes}м`;
      return `${minutes}м`;
    }

    async function loadSystemStatus() {
      const data = await api("/v1/system/status");
      if ($("fallbackVersion")) $("fallbackVersion").textContent = data.version || "н/д";
      if ($("fallbackUptime")) $("fallbackUptime").textContent = formatDuration(data.uptime_seconds);
      if ($("fallbackStorage")) {
        $("fallbackStorage").textContent = `${formatBytes(data.storage?.used_bytes)} / ${formatBytes(data.storage?.total_bytes)}`;
      }
      if ($("fallbackLoad")) $("fallbackLoad").textContent = data.load_average?.one_minute ?? "н/д";
      if ($("fallbackRss")) $("fallbackRss").textContent = data.process?.rss_mb != null ? `${data.process.rss_mb} MiB` : "н/д";
    }

    async function refreshAll() {
      await Promise.allSettled([listMemories(), loadConflicts(), loadVault(), loadModelSettings(), loadSystemStatus()]);
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
            <span class="pill ${isOpenReview(c.review_status) ? "warn" : "ok"}">${escapeHtml(reviewName(c.review_status))}</span>
            <strong>${escapeHtml(c.subject)} / ${escapeHtml(c.predicate)}</strong>
          </div>
          <p class="muted tiny">${escapeHtml(reasonName(c.suggested_reason))}</p>
          <div class="pill ok">рекомендация: ${escapeHtml(c.suggested_winner_value || "—")}</div>
          ${c.candidates.map(x => `<div class="card compact-card">
            <div style="display:flex;gap:10px;justify-content:space-between;align-items:center">
              <strong>${escapeHtml(x.value)}</strong>
              ${isOpenReview(c.review_status) ? `<button class="secondary" onclick="decideConflict(${jsString(c.id)}, 'overridden', ${jsString(x.value)}, ${jsString("Оператор выбрал: " + x.value)})">Выбрать</button>` : ""}
            </div>
            <div class="muted tiny">${escapeHtml(statusName(x.status))} · уверенность ${Number(x.confidence).toFixed(2)}</div>
            <div class="muted tiny">evidence: ${escapeHtml((x.evidence_ids || []).join(", ") || "нет")}</div>
          </div>`).join("")}
          ${isOpenReview(c.review_status) ? `<div class="actions">
            <button onclick="decideConflict(${jsString(c.id)}, 'accepted', ${jsString(c.suggested_winner_value)}, ${jsString(reasonName(c.suggested_reason))})">Принять рекомендацию</button>
            <button class="secondary" onclick="decideConflict(${jsString(c.id)}, 'dismissed', null, 'Оператор скрыл конфликт как неактуальный')">Скрыть как неактуальный</button>
          </div>` : ""}
        </div>`).join("") : `<div class="empty">Конфликтов нет. Память спокойна — подозрительно спокойна.</div>`;
    }

    async function decideConflict(caseId, status, winnerValue, defaultReason) {
      const reason = prompt("Причина решения по конфликту", defaultReason || "") || defaultReason || "operator decision";
      await api(`/v1/workspaces/${workspace()}/conflicts/${caseId}/decision`, {
        method: "PUT",
        body: JSON.stringify({
          tenant_id: tenant(),
          status,
          winner_value: winnerValue,
          reason,
        }),
      });
      log(status === "dismissed" ? "конфликт скрыт" : `конфликт решён: ${winnerValue || "без победителя"}`);
      await Promise.allSettled([loadConflicts(), listMemories()]);
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
      $("vaultEditor").value = sanitizeVaultEditableBody(file.editable_content ?? note.body);
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

    function jsString(value) {
      return JSON.stringify(value == null ? null : String(value));
    }

    function parseVaultNote(content) {
      const lines = String(content || "").split(/\\r?\\n/);
      if (lines[0] !== "---") return { frontmatter: {}, frontmatterBlock: "", body: content || "", tail: "" };
      const end = lines.findIndex((line, index) => index > 0 && line === "---");
      if (end < 0) return { frontmatter: {}, frontmatterBlock: "", body: content || "", tail: "" };
      const frontmatterLines = lines.slice(1, end);
      const frontmatter = parseFrontmatter(frontmatterLines);
      const bodyLines = lines.slice(end + 1);
      const sectionIndex = bodyLines.findIndex(isVaultSystemHeading);
      const editableBody = sanitizeVaultEditableBody((sectionIndex >= 0 ? bodyLines.slice(0, sectionIndex) : bodyLines).join("\\n"));
      const tail = sectionIndex >= 0 ? "\\n\\n" + bodyLines.slice(sectionIndex).join("\\n").trim() : "";
      return {
        frontmatter,
        frontmatterBlock: lines.slice(0, end + 1).join("\\n"),
        body: editableBody,
        tail,
      };
    }

    function isVaultSystemHeading(line) {
      const heading = String(line || "").trim().replace(/^#{2,6}\\s+/, "").trim().toLowerCase();
      return [
        "provenance", "quote", "links", "evidence",
        "embedding", "embeddings", "vector", "vectors", "vector data",
        "metadata", "frontmatter", "revision", "revisions",
        "technical", "system", "service data", "service", "debug", "diagnostics",
        "checksums", "checksums and signatures",
        "служебное", "служебные данные", "вектор", "векторы", "векторные данные",
        "embedding данные", "эмбеддинг", "эмбеддинги", "технические данные", "метаданные", "ревизии", "диагностика"
      ].includes(heading);
    }

    function sanitizeVaultEditableBody(value) {
      const lines = String(value || "").split(/\\r?\\n/);
      const kept = [];
      let droppingJsonBlock = false;
      let droppingFence = false;
      for (const line of lines) {
        const trimmed = line.trim();
        if (droppingFence) {
          if (trimmed.startsWith("```")) droppingFence = false;
          continue;
        }
        if (droppingJsonBlock) {
          if (/[}\\]]\\s*,?$/.test(trimmed)) droppingJsonBlock = false;
          continue;
        }
        if (!trimmed) {
          kept.push("");
          continue;
        }
        if (isVaultSystemHeading(line) || looksLikeVaultSystemField(trimmed) || looksLikeVectorPayload(trimmed) || looksLikeStructuredPayloadStart(trimmed)) {
          if (trimmed.startsWith("```")) {
            droppingFence = true;
            continue;
          }
          if (trimmed.endsWith("{") || trimmed.endsWith("[") || trimmed === "{" || trimmed === "[" || /^[{[]/.test(trimmed)) droppingJsonBlock = true;
          continue;
        }
        kept.push(line);
      }
      return kept.join("\\n").replace(/\\n{3,}/g, "\\n\\n").trim();
    }

    function looksLikeVaultSystemField(line) {
      return /^(embedding|embeddings|vector|vectors|metadata|provenance|revision|revisions|checksum_sha256|checksum|source|origin|object|supersedes|superseded_by|tenant_id|workspace_id|item_id|id|payload|point|points|qdrant|dense|sparse|values|dimension|dimensions|dim|model|model_name|provider|score|distance|created_at|updated_at|valid_from|valid_to|observed_at|labels|confidence|importance|status|type)\\s*[:=]/i.test(line)
        || /^(эмбеддинг|эмбеддинги|вектор|векторы|метаданные|ревизия|ревизии|источник|контрольная сумма|служебные данные|размерность|модель|провайдер)\\s*[:=]/i.test(line)
        || /^["']?(embedding|embeddings|vector|vectors|metadata|provenance|revision|checksum_sha256|payload|qdrant|dimension|model_name)["']?\\s*:/i.test(line);
    }

    function looksLikeVectorPayload(line) {
      if (/^\\[\\s*-?\\d+(\\.\\d+)?([eE][+-]?\\d+)?(\\s*,\\s*-?\\d+(\\.\\d+)?([eE][+-]?\\d+)?){3,}\\s*,?\\s*\\]?$/.test(line)) return true;
      if (/^[-+]?\\d+(\\.\\d+)?([eE][+-]?\\d+)?(\\s*,\\s*[-+]?\\d+(\\.\\d+)?([eE][+-]?\\d+)?){5,}$/.test(line)) return true;
      if (/^[-+]?\\d+(\\.\\d+)?([eE][+-]?\\d+)?(\\s+[-+]?\\d+(\\.\\d+)?([eE][+-]?\\d+)?){8,}$/.test(line)) return true;
      return false;
    }

    function looksLikeStructuredPayloadStart(line) {
      const lowered = line.toLowerCase();
      if (lowered.startsWith("```") && /(json|yaml|yml|embedding|vector|qdrant)/.test(lowered)) return true;
      if (!/^[{[]/.test(lowered)) return false;
      return /(embedding|embeddings|vector|vectors|payload|qdrant|metadata|provenance|dimension|model_name)/.test(lowered);
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
      const cleanBody = sanitizeVaultEditableBody(String(body || ""));
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
      if ($("referenceGraph")) {
        $("referenceGraph").innerHTML = renderReferenceGraphScene();
      }
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

    function renderReferenceGraphScene() {
      const cx = 220;
      const cy = 178;
      const nodes = [
        { label: "Response\\nPreferences", x: 72, y: 112, r: 38, cls: "core" },
        { label: "Communication\\nStyle", x: 220, y: 58, r: 34, cls: "semantic" },
        { label: "AI Memory\\nSystem Project", x: 370, y: 120, r: 36, cls: "core" },
        { label: "Tools &\\nSoftware", x: 78, y: 258, r: 34, cls: "context" },
        { label: "Obsidian\\nPreference", x: 220, y: 304, r: 34, cls: "semantic" },
        { label: "Context &\\nEnvironment", x: 365, y: 258, r: 34, cls: "context" },
      ];
      const spokes = nodes.map(node => `<line class="edge-line ok" x1="${cx}" y1="${cy}" x2="${node.x}" y2="${node.y}"></line>`).join("");
      const satellites = Array.from({ length: 26 }, (_, index) => {
        const angle = index * Math.PI * 2 / 26;
        const radius = 122 + (index % 4) * 16;
        const x = cx + Math.cos(angle) * radius;
        const y = cy + Math.sin(angle) * radius;
        const opacity = .25 + (index % 5) * .09;
        return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${index % 3 === 0 ? 5 : 3.5}" fill="#3b82f6" opacity="${opacity.toFixed(2)}"></circle>
          <line class="edge-line" x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" opacity=".18"></line>`;
      }).join("");
      const renderedNodes = nodes.map(node => {
        const fill = node.cls === "semantic" ? "url(#refSemantic)" : node.cls === "context" ? "url(#refContext)" : "url(#refCore)";
        return `<g>
          <circle cx="${node.x}" cy="${node.y}" r="${node.r}" fill="rgba(2,8,23,.86)" stroke="rgba(96,165,250,.55)"></circle>
          <circle cx="${node.x}" cy="${node.y}" r="${node.r - 2}" fill="${fill}" opacity=".42"></circle>
          ${node.label.split("\\n").map((line, i) => `<text class="node-label" x="${node.x}" y="${node.y - 4 + i * 13}">${escapeHtml(line)}</text>`).join("")}
        </g>`;
      }).join("");
      return `<svg class="force-svg" viewBox="0 0 440 360" role="img" aria-label="Memory graph preview">
        <defs>
          <radialGradient id="refCenter"><stop offset="0%" stop-color="#93c5fd"></stop><stop offset="58%" stop-color="#2563eb"></stop><stop offset="100%" stop-color="#020617"></stop></radialGradient>
          <linearGradient id="refCore" x1="0" x2="1"><stop stop-color="#0ea5e9"></stop><stop offset="1" stop-color="#2563eb"></stop></linearGradient>
          <linearGradient id="refSemantic" x1="0" x2="1"><stop stop-color="#7c3aed"></stop><stop offset="1" stop-color="#a855f7"></stop></linearGradient>
          <linearGradient id="refContext" x1="0" x2="1"><stop stop-color="#0891b2"></stop><stop offset="1" stop-color="#22d3ee"></stop></linearGradient>
        </defs>
        ${satellites}
        ${spokes}
        ${renderedNodes}
        <g>
          <circle cx="${cx}" cy="${cy}" r="58" fill="url(#refCenter)" stroke="#60a5fa" stroke-width="2"></circle>
          <circle cx="${cx}" cy="${cy}" r="67" fill="none" stroke="rgba(59,130,246,.36)"></circle>
          <text class="node-label" x="${cx}" y="${cy - 8}">User prefers</text>
          <text class="node-label" x="${cx}" y="${cy + 6}">concise, bullet-point</text>
          <text class="node-label" x="${cx}" y="${cy + 20}">answers</text>
        </g>
      </svg>`;
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
        unresolved: "требует решения", open: "открыто", accepted: "принято", dismissed: "скрыто", rejected: "отклонено", overridden: "переопределено"
      })[value] || value;
    }

    function isOpenReview(value) {
      return value === "unresolved" || value === "pending" || value === "open";
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
