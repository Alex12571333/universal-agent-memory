"""Hermes MemoryProvider for Obelisk Memory.

Install by copying this directory to ``$HERMES_HOME/plugins/universal_agent_memory``
and setting ``memory.provider: universal_agent_memory`` in Hermes config.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

if TYPE_CHECKING:
    class MemoryProvider:
        """Static fallback for repository type checks."""

else:
    try:  # pragma: no cover - available inside Hermes runtime.
        from agent.memory_provider import MemoryProvider
    except Exception:  # pragma: no cover - lets repository tests import the provider.
        class MemoryProvider:
            """Fallback base used outside Hermes."""

            pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _stable_uuid(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"universal-agent-memory:{label}")


def _uuid_env(name: str, fallback: str) -> UUID:
    raw = os.getenv(name)
    return UUID(raw) if raw else _stable_uuid(fallback)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


class UniversalAgentMemoryProvider(MemoryProvider):
    """Hermes native memory provider backed by the UAM Docker server."""

    def __init__(self) -> None:
        self._url = os.getenv("UAM_URL", "http://localhost:6798").rstrip("/")
        self._api_key = os.getenv("UAM_API_KEY", "")
        self._enabled = _env_bool("UAM_MEMORY_ENABLED", True)
        self._tenant_id = _uuid_env("UAM_TENANT_ID", "tenant:default")
        self._workspace_id = _uuid_env("UAM_WORKSPACE_ID", f"workspace:{os.getcwd()}")
        self._agent_id = _uuid_env("UAM_AGENT_ID", f"agent:hermes:{os.getenv('USER', 'hermes')}")
        self._thread_id = _stable_uuid("thread:hermes")
        self._top_k = int(os.getenv("UAM_MEMORY_RECALL_TOP_K", "8"))
        self._context_budget_tokens = int(os.getenv("UAM_CONTEXT_BUDGET_TOKENS", "131072"))
        self._labels: tuple[str, ...] = ("hermes",)

    @property
    def name(self) -> str:
        return "universal_agent_memory"

    def is_available(self) -> bool:
        return bool(self._enabled and self._url)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        platform = str(kwargs.get("platform") or "cli")
        identity = str(
            kwargs.get("agent_identity") or kwargs.get("user_id") or os.getenv("USER", "hermes")
        )
        self._thread_id = _stable_uuid(f"thread:hermes:{platform}:{session_id}")
        self._agent_id = _uuid_env("UAM_AGENT_ID", f"agent:hermes:{identity}")
        self._labels = tuple(
            item
            for item in (
                "hermes",
                platform,
                str(kwargs.get("agent_context") or ""),
                str(kwargs.get("agent_workspace") or ""),
            )
            if item
        )

    def system_prompt_block(self) -> str:
        return (
            "# Obelisk Memory\n"
            "Active. Relevant long-term memory is injected before turns. "
            "Use the universal_agent_memory_* tools for explicit memory inspection or writes."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._enabled or not query.strip():
            return ""
        payload = self._base_payload()
        payload.update(
            {
                "query": query,
                "operation": "hermes_prefetch",
                "top_k": self._top_k,
                "context_budget_tokens": self._context_budget_tokens,
            }
        )
        try:
            data = self._post_json("/v1/memory/recall", payload)
        except RuntimeError:
            return ""
        markdown = str(data.get("context", {}).get("markdown", "")).strip()
        return f"## Obelisk Memory\n{markdown}" if markdown else ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        if not self._enabled:
            return
        user_text = user_content.strip()
        assistant_text = assistant_content.strip()
        if not user_text and not assistant_text:
            return
        turn_messages = []
        if user_text:
            turn_messages.append({"role": "user", "content": user_text})
        if assistant_text:
            turn_messages.append({"role": "assistant", "content": assistant_text})
        self._append_conversation_turn(
            messages=turn_messages,
            session_id=session_id,
            idempotency_key=(
                f"hermes-turn:{session_id or self._thread_id}:"
                f"{_digest(json.dumps(turn_messages, ensure_ascii=False))}"
            ),
        )

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        summary = _summarize_messages(messages)
        if not summary:
            return
        self._retain(
            layer="episodic",
            kind="session_summary",
            text=summary,
            idempotency_key=f"hermes-session:{self._thread_id}:{_digest(summary)}",
        )

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [SEARCH_SCHEMA, ADD_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if tool_name == "universal_agent_memory_search":
            query = str(args.get("query", "")).strip()
            if not query:
                return json.dumps({"error": "Missing query"})
            return json.dumps({"result": self.prefetch(query)})
        if tool_name == "universal_agent_memory_add":
            content = str(args.get("content", "")).strip()
            if not content:
                return json.dumps({"error": "Missing content"})
            result = self._propose_memory(
                target="fact",
                proposal=content,
                evidence=str(args.get("evidence") or "Hermes explicit memory tool call"),
                idempotency_key=f"hermes-proposal:{_digest(content)}",
            )
            return json.dumps({"result": "Memory proposal stored.", "id": result.get("id")})
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    def _base_payload(self) -> dict[str, Any]:
        return {
            "tenant_id": str(self._tenant_id),
            "workspace_id": str(self._workspace_id),
            "agent_id": str(self._agent_id),
            "thread_id": str(self._thread_id),
            "labels": list(self._labels),
        }

    def _retain(
        self,
        *,
        layer: str,
        kind: str,
        text: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = self._base_payload()
        payload.update(
            {
                "layer": layer,
                "scope": "thread",
                "kind": kind,
                "text": text,
                "source_kind": "hermes-memory-provider",
                "idempotency_key": idempotency_key,
            }
        )
        return self._post_json("/v1/memory/retain", payload)

    def _append_conversation_turn(
        self,
        *,
        messages: list[dict[str, Any]],
        idempotency_key: str,
        session_id: str = "",
    ) -> dict[str, Any]:
        payload = self._base_payload()
        payload.update(
            {
                "namespace": "hermes",
                "source_kind": "hermes-memory-provider",
                "retention_policy": "raw_and_curated",
                "messages": messages,
                "metadata": {"session_id": session_id},
                "idempotency_key": idempotency_key,
            }
        )
        return self._post_json("/v1/conversations/turns", payload)

    def _propose_memory(
        self,
        *,
        target: str,
        proposal: str,
        evidence: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = self._base_payload()
        payload.update(
            {
                "namespace": "hermes",
                "requester": "hermes-memory-provider",
                "target": target,
                "proposal": proposal,
                "evidence": evidence,
                "confidence": 0.7,
                "importance": 0.5,
                "idempotency_key": idempotency_key,
            }
        )
        return self._post_json("/v1/memory/proposals", payload)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(
            f"{self._url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"UAM HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"UAM request failed: {exc.reason}") from exc
        parsed = json.loads(raw or "{}")
        if not isinstance(parsed, dict):
            raise RuntimeError("UAM returned non-object JSON")
        return parsed


def _summarize_messages(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages[-12:]:
        role = str(message.get("role", "message"))
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        text = str(content).strip()
        if text:
            parts.append(f"{role}: {text}")
    return "\n\n".join(parts)[-8000:]


SEARCH_SCHEMA = {
    "name": "universal_agent_memory_search",
    "description": "Search Obelisk Memory for relevant cross-agent context.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
        "required": ["query"],
    },
}

ADD_SCHEMA = {
    "name": "universal_agent_memory_add",
    "description": "Submit an explicit durable fact proposal to Obelisk Memory.",
    "parameters": {
        "type": "object",
        "properties": {"content": {"type": "string"}, "evidence": {"type": "string"}},
        "required": ["content"],
    },
}


def register(ctx: Any) -> None:
    """Register with the current Hermes exclusive memory-provider loader."""
    ctx.register_memory_provider(UniversalAgentMemoryProvider())


def register_memory_provider() -> UniversalAgentMemoryProvider:
    """Compatibility factory for older Hermes integration tests."""
    return UniversalAgentMemoryProvider()
