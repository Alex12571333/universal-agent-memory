"""Thin Corax ``MemoryProvider`` adapter for the UAM HTTP service."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core import (
    CoreError,
    ErrorCode,
    HealthStatus,
    MemoryProvider,
    MemoryQuery,
    MemoryRecord,
    PermissionLevel,
    Result,
    RiskLevel,
)
from agent_sdk import memory_provider


class _Client(Protocol):
    def retain(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def recall(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def health(self) -> bool: ...


class _HttpClient:
    def __init__(self, url: str, api_key: str | None) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key

    def retain(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/memory/retain", payload)

    def recall(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/memory/recall", payload)

    def health(self) -> bool:
        try:
            self._request("GET", "/health", None)
        except RuntimeError:
            return False
        return True

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.url}{path}",
            data=(
                json.dumps(payload).encode("utf-8")
                if payload is not None
                else None
            ),
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310
                decoded = json.loads(response.read().decode("utf-8") or "{}")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"UAM HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"UAM request failed: {exc}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("UAM returned a non-object response")
        return decoded


@memory_provider(
    id="memory.uam",
    name="Universal Agent Memory",
    description="Durable scoped memory backed by a self-hosted UAM service.",
    version="0.2.0",
    tags=("memory", "uam", "durable", "self-hosted"),
    interfaces=("agent.memory/v1",),
    permission_level=PermissionLevel.CONFIRM,
    required_scopes=("memory.read", "memory.write", "network.outbound"),
    risk_level=RiskLevel.MEDIUM,
    side_effects=("network_request", "memory_write"),
    secrets=("UAM_API_KEY",),
    config_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "tenant_id": {"type": "string"},
            "workspace_id": {"type": "string"},
            "agent_id": {"type": "string"},
        },
    },
    entrypoint="provider:UniversalAgentMemoryProvider",
    min_core_version="0.2.0",
)
class UniversalAgentMemoryProvider(MemoryProvider):
    """Map the Corax memory port to UAM retain/recall endpoints."""

    def __init__(self, *, client: _Client | None = None) -> None:
        self._client = client or _HttpClient(
            os.getenv("UAM_URL", "http://localhost:6798"),
            os.getenv("UAM_API_KEY") or None,
        )

    async def remember(self, record: MemoryRecord) -> Result:
        scope = dict(record.scope)
        payload = {
            "tenant_id": str(_identity(scope, "tenant_id", "UAM_TENANT_ID", "tenant")),
            "workspace_id": str(
                _identity(scope, "workspace_id", "UAM_WORKSPACE_ID", "workspace")
            ),
            "agent_id": str(_identity(scope, "agent_id", "UAM_AGENT_ID", "corax")),
            "layer": str(record.metadata.get("layer", "semantic")),
            "scope": str(scope.get("scope", "workspace")),
            "kind": record.kind,
            "text": record.content,
            "labels": list(record.metadata.get("labels", ())),
            "source_kind": "corax-memory-provider",
        }
        thread_id = scope.get("thread_id")
        if thread_id:
            payload["thread_id"] = str(thread_id)
        if record.idempotency_key:
            payload["idempotency_key"] = record.idempotency_key
        try:
            data = await asyncio.to_thread(self._client.retain, payload)
        except Exception as exc:  # noqa: BLE001
            return _failure(str(exc))
        return Result.ok(data, session_id="")

    async def recall(self, query: MemoryQuery) -> Result:
        scope = dict(query.scopes[0]) if query.scopes else {}
        payload = {
            "tenant_id": str(_identity(scope, "tenant_id", "UAM_TENANT_ID", "tenant")),
            "workspace_id": str(
                _identity(scope, "workspace_id", "UAM_WORKSPACE_ID", "workspace")
            ),
            "agent_id": str(_identity(scope, "agent_id", "UAM_AGENT_ID", "corax")),
            "query": query.text,
            "operation": str(query.metadata.get("operation", "corax_memory_recall")),
            "top_k": query.limit,
            "context_budget_tokens": int(
                query.metadata.get("context_budget_tokens", 1200)
            ),
            "context_per_layer_limit": int(
                query.metadata.get("context_per_layer_limit", 3)
            ),
            "minimum_score": float(query.metadata.get("minimum_score", 0.45)),
            "labels": list(query.metadata.get("labels", ())),
        }
        thread_id = scope.get("thread_id")
        if thread_id:
            payload["thread_id"] = str(thread_id)
        try:
            data = await asyncio.to_thread(self._client.recall, payload)
        except Exception as exc:  # noqa: BLE001
            return _failure(str(exc))
        return Result.ok(data, session_id="")

    async def forget(self, memory_id: str, *, scope: dict | None = None) -> Result:
        return Result.fail(
            CoreError(
                ErrorCode.INVALID_INPUT,
                "UAM does not expose destructive forget; use its reviewed "
                "supersede/privacy workflow",
                {"memory_id": memory_id},
            ),
            session_id="",
        )

    async def health_check(self) -> HealthStatus:
        healthy = await asyncio.to_thread(self._client.health)
        return HealthStatus.HEALTHY if healthy else HealthStatus.DEGRADED


def _identity(
    scope: dict[str, Any],
    key: str,
    env_name: str,
    fallback: str,
) -> UUID:
    raw = scope.get(key) or os.getenv(env_name)
    if raw:
        return UUID(str(raw))
    return uuid5(NAMESPACE_URL, f"universal-agent-memory:{fallback}")


def _failure(message: str) -> Result:
    return Result.fail(
        CoreError(ErrorCode.CAPABILITY_FAILED, message),
        session_id="",
    )
