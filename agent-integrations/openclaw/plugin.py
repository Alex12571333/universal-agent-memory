"""OpenClaw-facing factory for Obelisk Memory native plugin hooks."""

from __future__ import annotations

from shared.config import AgentMemoryConfig
from shared.plugin import UniversalAgentMemoryPlugin, build_plugin


def create_plugin(config: AgentMemoryConfig | None = None) -> UniversalAgentMemoryPlugin:
    """Create the OpenClaw memory plugin core.

    OpenClaw runtime bindings should map concrete lifecycle callbacks onto the
    returned `UniversalAgentMemoryPlugin` methods.
    """
    return build_plugin(config or AgentMemoryConfig.from_env(integration_name="openclaw"))
