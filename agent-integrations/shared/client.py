"""Small stdlib HTTP client used by native agent plugins."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from shared.config import AgentMemoryConfig


@dataclass(frozen=True, slots=True)
class RetainedMemory:
    """Identity returned after a memory write."""

    id: UUID
    revision: int
    created: bool


class MemoryClient(Protocol):
    """Protocol implemented by HTTP and test memory clients."""

    def recall(
        self,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        query: str,
        operation: str,
        top_k: int,
        context_budget_tokens: int,
        labels: tuple[str, ...] = (),
        agent_id: UUID | None = None,
        thread_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Recall a context package."""
        ...

    def retain(
        self,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        layer: str,
        scope: str,
        kind: str,
        text: str,
        labels: tuple[str, ...] = (),
        agent_id: UUID | None = None,
        thread_id: UUID | None = None,
        source_kind: str = "agent-plugin",
        idempotency_key: str | None = None,
    ) -> RetainedMemory:
        """Retain one memory item."""
        ...

    def save_checkpoint(
        self,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        thread_id: UUID,
        state: dict[str, Any],
    ) -> UUID:
        """Persist checkpoint state."""
        ...

    def reflect(self, *, tenant_id: UUID, workspace_id: UUID) -> None:
        """Trigger reflection."""
        ...


class MemoryServerClient:
    """Minimal Universal Agent Memory HTTP client for plugin runtimes."""

    def __init__(self, config: AgentMemoryConfig) -> None:
        """Bind the client to one memory server."""
        self._config = config

    def recall(
        self,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        query: str,
        operation: str,
        top_k: int,
        context_budget_tokens: int,
        labels: tuple[str, ...] = (),
        agent_id: UUID | None = None,
        thread_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Call `/v1/memory/recall` and return the decoded response."""
        payload: dict[str, Any] = {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id),
            "query": query,
            "operation": operation,
            "top_k": top_k,
            "context_budget_tokens": context_budget_tokens,
            "labels": list(labels),
        }
        if agent_id:
            payload["agent_id"] = str(agent_id)
        if thread_id:
            payload["thread_id"] = str(thread_id)
        return self._post_json("/v1/memory/recall", payload)

    def retain(
        self,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        layer: str,
        scope: str,
        kind: str,
        text: str,
        labels: tuple[str, ...] = (),
        agent_id: UUID | None = None,
        thread_id: UUID | None = None,
        source_kind: str = "agent-plugin",
        idempotency_key: str | None = None,
    ) -> RetainedMemory:
        """Append one durable memory through `/v1/memory/retain`."""
        payload: dict[str, Any] = {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id),
            "layer": layer,
            "scope": scope,
            "kind": kind,
            "text": text,
            "source_kind": source_kind,
            "labels": list(labels),
        }
        if agent_id:
            payload["agent_id"] = str(agent_id)
        if thread_id:
            payload["thread_id"] = str(thread_id)
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        data = self._post_json("/v1/memory/retain", payload)
        return RetainedMemory(
            id=UUID(str(data["id"])),
            revision=int(data["revision"]),
            created=bool(data["created"]),
        )

    def save_checkpoint(
        self,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        thread_id: UUID,
        state: dict[str, Any],
    ) -> UUID:
        """Persist a working-state checkpoint."""
        data = self._post_json(
            "/v1/checkpoints",
            {
                "tenant_id": str(tenant_id),
                "workspace_id": str(workspace_id),
                "thread_id": str(thread_id),
                "state": state,
            },
        )
        return UUID(str(data["id"]))

    def reflect(self, *, tenant_id: UUID, workspace_id: UUID) -> None:
        """Trigger reflection after a completed agent run."""
        self._post_json(
            f"/v1/workspaces/{workspace_id}/reflect?tenant_id={tenant_id}",
            {},
        )

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._config.url}{path}"
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"memory server HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"memory server request failed: {exc.reason}") from exc
        parsed = json.loads(raw or "{}")
        if not isinstance(parsed, dict):
            raise RuntimeError("memory server returned non-object JSON")
        return parsed
