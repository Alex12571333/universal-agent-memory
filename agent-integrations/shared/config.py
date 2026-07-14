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
    recall_mode: str = "adaptive"
    recall_top_k: int = 6
    context_budget_tokens: int = 1200
    context_per_layer_limit: int = 3
    recall_minimum_score: float = 0.45
    research_recall_top_k: int = 10
    research_context_budget_tokens: int = 2500
    research_context_per_layer_limit: int = 6
    force_full_recall: bool = False
    retain_tool_traces: bool = True
    retain_messages: bool = True
    retain_errors: bool = True

    @classmethod
    def from_env(cls, *, integration_name: str = "native") -> AgentMemoryConfig:
        """Load plugin settings from `UAM_*` environment variables."""
        return cls(
            url=os.getenv("UAM_URL", "http://localhost:6798").rstrip("/"),
            api_key=os.getenv("UAM_API_KEY") or None,
            enabled=_env_bool("UAM_MEMORY_ENABLED", default=True),
            integration_name=os.getenv("UAM_AGENT_INTEGRATION", integration_name),
            recall_mode=os.getenv("UAM_RECALL_MODE", "adaptive").strip().lower(),
            recall_top_k=int(os.getenv("UAM_MEMORY_RECALL_TOP_K", "6")),
            context_budget_tokens=int(os.getenv("UAM_CONTEXT_BUDGET_TOKENS", "1200")),
            context_per_layer_limit=int(os.getenv("UAM_CONTEXT_PER_LAYER_LIMIT", "3")),
            recall_minimum_score=float(os.getenv("UAM_RECALL_MINIMUM_SCORE", "0.45")),
            research_recall_top_k=int(os.getenv("UAM_RESEARCH_RECALL_TOP_K", "10")),
            research_context_budget_tokens=int(
                os.getenv("UAM_RESEARCH_CONTEXT_BUDGET_TOKENS", "2500")
            ),
            research_context_per_layer_limit=int(
                os.getenv("UAM_RESEARCH_CONTEXT_PER_LAYER_LIMIT", "6")
            ),
            force_full_recall=_env_bool("UAM_FORCE_FULL_RECALL", default=False),
            retain_tool_traces=_env_bool("UAM_RETAIN_TOOL_TRACES", default=True),
            retain_messages=_env_bool("UAM_RETAIN_MESSAGES", default=True),
            retain_errors=_env_bool("UAM_RETAIN_ERRORS", default=True),
        )


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
