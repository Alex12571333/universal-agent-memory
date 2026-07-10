from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID, uuid4

from pytest import MonkeyPatch

ROOT = Path(__file__).resolve().parents[1]
INTEGRATIONS = ROOT / "agent-integrations"
sys.path.insert(0, str(INTEGRATIONS))

from hermes.plugin import create_plugin as create_hermes_plugin  # noqa: E402
from hermes.universal_agent_memory import (  # noqa: E402
    UniversalAgentMemoryProvider,
    register,
    register_memory_provider,
)
from openclaw.plugin import create_plugin as create_openclaw_plugin  # noqa: E402
from shared.client import RetainedMemory  # noqa: E402
from shared.config import AgentMemoryConfig  # noqa: E402
from shared.identity import resolve_workspace_id  # noqa: E402
from shared.lifecycle import AgentEventKind, AgentLifecycleEvent, AgentRunContext  # noqa: E402
from shared.plugin import UniversalAgentMemoryPlugin  # noqa: E402


class FakeMemoryClient:
    def __init__(self) -> None:
        self.retained: list[dict[str, object]] = []
        self.recalled: list[dict[str, object]] = []
        self.checkpoints: list[dict[str, object]] = []

    def recall(self, **kwargs: object) -> dict[str, object]:
        self.recalled.append(kwargs)
        return {
            "sources_used": ["postgres"],
            "context": {
                "markdown": "## Memory\n- Existing fact",
                "trace_ids": [str(uuid4())],
            },
        }

    def retain(self, **kwargs: object) -> RetainedMemory:
        self.retained.append(kwargs)
        return RetainedMemory(id=uuid4(), revision=1, created=True)

    def save_checkpoint(self, **kwargs: object) -> UUID:
        self.checkpoints.append(kwargs)
        return uuid4()

def test_before_agent_run_recalls_context_package() -> None:
    client = FakeMemoryClient()
    plugin = UniversalAgentMemoryPlugin(client, AgentMemoryConfig(integration_name="test"))
    context = AgentRunContext(
        tenant_id=uuid4(),
        workspace_id=uuid4(),
        agent_id=uuid4(),
        thread_id=uuid4(),
        operation="plan",
        labels=("alpha",),
    )

    injection = plugin.before_agent_run(context)

    assert "Existing fact" in injection.markdown
    assert injection.sources_used == ("postgres",)
    assert client.recalled[0]["operation"] == "plan"
    assert client.recalled[0]["labels"] == ("alpha",)


def test_after_tool_event_retains_procedural_memory() -> None:
    client = FakeMemoryClient()
    plugin = UniversalAgentMemoryPlugin(client, AgentMemoryConfig())
    context = AgentRunContext(tenant_id=uuid4(), workspace_id=uuid4(), thread_id=uuid4())
    event = AgentLifecycleEvent(
        kind=AgentEventKind.TOOL_CALL,
        text="searched repository",
        context=context,
        tool_name="ripgrep",
    )

    retained_ids = plugin.after_event(event)

    assert len(retained_ids) == 1
    retained = client.retained[0]
    assert retained["layer"] == "procedural"
    assert retained["kind"] == "tool_trace"
    assert retained["scope"] == "thread"
    assert "ripgrep" in str(retained["text"])
    assert str(context.thread_id) in str(retained["idempotency_key"])


def test_run_complete_retains_summary_without_operator_actions() -> None:
    client = FakeMemoryClient()
    plugin = UniversalAgentMemoryPlugin(client, AgentMemoryConfig())
    context = AgentRunContext(tenant_id=uuid4(), workspace_id=uuid4())

    ids = plugin.on_run_complete(context, "Implemented memory plugin.")

    assert len(ids) == 1
    assert client.retained[0]["kind"] == "run_summary"


def test_disabled_plugin_does_not_call_memory_server() -> None:
    client = FakeMemoryClient()
    plugin = UniversalAgentMemoryPlugin(client, AgentMemoryConfig(enabled=False))
    context = AgentRunContext(tenant_id=uuid4(), workspace_id=uuid4())

    assert plugin.before_agent_run(context).markdown == ""
    assert plugin.on_run_complete(context, "summary") == ()
    assert client.recalled == []
    assert client.retained == []


def test_openclaw_and_hermes_factories_accept_explicit_config() -> None:
    config = AgentMemoryConfig(enabled=False)

    assert isinstance(create_openclaw_plugin(config), UniversalAgentMemoryPlugin)
    assert isinstance(create_hermes_plugin(config), UniversalAgentMemoryPlugin)


def test_identity_fallbacks_are_stable(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("UAM_WORKSPACE_ID", raising=False)

    first = resolve_workspace_id(fallback="repo-a")
    second = resolve_workspace_id(fallback="repo-a")

    assert first == second


def test_hermes_provider_discovery_and_tools(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UAM_MEMORY_ENABLED", "true")
    provider = register_memory_provider()

    assert isinstance(provider, UniversalAgentMemoryProvider)
    assert provider.name == "universal_agent_memory"
    assert provider.is_available()
    assert {schema["name"] for schema in provider.get_tool_schemas()} == {
        "universal_agent_memory_search",
        "universal_agent_memory_add",
    }

    registered: list[UniversalAgentMemoryProvider] = []

    class Context:
        def register_memory_provider(self, candidate: UniversalAgentMemoryProvider) -> None:
            registered.append(candidate)

    register(Context())
    assert len(registered) == 1
    assert isinstance(registered[0], UniversalAgentMemoryProvider)


def test_openclaw_installable_plugin_package_exists() -> None:
    package_json = ROOT / "agent-integrations" / "openclaw" / "plugin" / "package.json"
    manifest = ROOT / "agent-integrations" / "openclaw" / "plugin" / "openclaw.plugin.json"
    index_js = ROOT / "agent-integrations" / "openclaw" / "plugin" / "index.js"

    assert package_json.exists()
    assert manifest.exists()
    assert index_js.exists()
    assert "openclaw" in package_json.read_text()
    index = index_js.read_text()
    assert "registerHook(\"agent_turn_prepare\"" in index
    assert "obelisk-memory-recall" in index
    assert "definePluginEntry" not in index
    assert "export default {" in index
