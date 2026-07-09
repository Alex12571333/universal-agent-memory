"""Configuration for native Obelisk Memory plugins."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentMemoryConfig:
    """Runtime-independent memory plugin settings."""

    url: str = "http://localhost:6798"
    api_key: str | None = None
    enabled: bool = True
    integration_name: str = "native"
    recall_top_k: int = 8
    context_budget_tokens: int = 131072
    retain_tool_traces: bool = True
    retain_messages: bool = True
    retain_errors: bool = True
    trigger_reflection_on_complete: bool = False

    @classmethod
    def from_env(cls, *, integration_name: str = "native") -> AgentMemoryConfig:
        """Load plugin settings from `UAM_*` environment variables."""
        return cls(
            url=os.getenv("UAM_URL", "http://localhost:6798").rstrip("/"),
            api_key=os.getenv("UAM_API_KEY") or None,
            enabled=_env_bool("UAM_MEMORY_ENABLED", default=True),
            integration_name=os.getenv("UAM_AGENT_INTEGRATION", integration_name),
            recall_top_k=int(os.getenv("UAM_MEMORY_RECALL_TOP_K", "8")),
            context_budget_tokens=int(os.getenv("UAM_CONTEXT_BUDGET_TOKENS", "131072")),
            retain_tool_traces=_env_bool("UAM_RETAIN_TOOL_TRACES", default=True),
            retain_messages=_env_bool("UAM_RETAIN_MESSAGES", default=True),
            retain_errors=_env_bool("UAM_RETAIN_ERRORS", default=True),
            trigger_reflection_on_complete=_env_bool(
                "UAM_REFLECT_ON_RUN_COMPLETE",
                default=False,
            ),
        )


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
