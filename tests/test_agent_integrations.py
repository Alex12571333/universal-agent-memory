from __future__ import annotations

import json
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
    register_memory_provider,
)
from hermes.universal_agent_memory.recall_gate import (  # noqa: E402
    evaluate_recall_gate as evaluate_hermes_gate,
)
from openclaw.plugin import create_plugin as create_openclaw_plugin  # noqa: E402
from shared.client import RetainedMemory  # noqa: E402
from shared.config import AgentMemoryConfig  # noqa: E402
from shared.identity import resolve_workspace_id  # noqa: E402
from shared.lifecycle import AgentEventKind, AgentLifecycleEvent, AgentRunContext  # noqa: E402
from shared.plugin import UniversalAgentMemoryPlugin  # noqa: E402
from shared.recall_gate import evaluate_recall_gate as evaluate_shared_gate  # noqa: E402


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
        metadata={"prompt": "What remains in our project?"},
    )

    injection = plugin.before_agent_run(context)

    assert "Existing fact" in injection.markdown
    assert injection.sources_used == ("postgres",)
    assert client.recalled[0]["operation"] == "plan"
    assert client.recalled[0]["labels"] == ("alpha",)
    assert client.recalled[0]["top_k"] == 6
    assert client.recalled[0]["context_budget_tokens"] == 1200
    assert client.recalled[0]["context_per_layer_limit"] == 3
    assert client.recalled[0]["minimum_score"] == 0.45
    assert "untrusted reference data" in injection.markdown


def test_shared_plugin_gate_skips_greeting_without_http_recall() -> None:
    client = FakeMemoryClient()
    plugin = UniversalAgentMemoryPlugin(client, AgentMemoryConfig())
    context = AgentRunContext(
        tenant_id=uuid4(),
        workspace_id=uuid4(),
        metadata={"prompt": "Привет!"},
    )

    assert plugin.before_agent_run(context).markdown == ""
    assert client.recalled == []
    assert plugin.recall_gate_metrics()["decisions"] == {"skip:greeting:none": 1}


def test_shared_plugin_always_mode_uses_research_tier() -> None:
    client = FakeMemoryClient()
    plugin = UniversalAgentMemoryPlugin(client, AgentMemoryConfig(recall_mode="always"))
    context = AgentRunContext(
        tenant_id=uuid4(),
        workspace_id=uuid4(),
        metadata={"prompt": "Write a fully self-contained answer."},
    )

    plugin.before_agent_run(context)

    assert client.recalled[0]["top_k"] == 10
    assert client.recalled[0]["context_budget_tokens"] == 2500
    assert client.recalled[0]["context_per_layer_limit"] == 6


def test_python_gates_match_shared_ru_en_contract() -> None:
    cases = json.loads(
        (INTEGRATIONS / "shared" / "recall_gate_cases.json").read_text(encoding="utf-8")
    )
    for case in cases:
        options = {
            "mode": case.get("mode", "adaptive"),
            "has_live_context": case.get("has_live_context"),
            "force_full_recall": case.get("force_full_recall", False),
        }
        expected = case["expected"]
        for evaluator in (evaluate_shared_gate, evaluate_hermes_gate):
            decision = evaluator(case["query"], **options)
            assert {
                "should_recall": decision.should_recall,
                "reason": decision.reason,
                "tier": decision.tier,
            } == expected


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


def test_hermes_provider_uses_provisioned_thread_id(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UAM_THREAD_ID", "00000000-0000-0000-0000-000000000121")

    provider = UniversalAgentMemoryProvider()
    provider.initialize("different-session", platform="cli")

    assert str(provider._thread_id) == "00000000-0000-0000-0000-000000000121"


def test_hermes_search_tool_returns_ranked_records_not_ambiguous_markdown(
    monkeypatch: MonkeyPatch,
) -> None:
    provider = UniversalAgentMemoryProvider()
    observed: dict[str, object] = {}

    def recall(path: str, payload: dict[str, object]) -> dict[str, object]:
        observed["path"] = path
        observed["payload"] = payload
        return {
            "context": {"markdown": "## semantic\n- irrelevant formatting"},
            "results": [
                {
                    "layer": "semantic",
                    "text": "OBELISK-EXACT-MARKER",
                    "source": "postgres_lexical",
                    "score": 0.987654321,
                },
                {"layer": "episodic", "text": "older turn", "score": 0.25},
            ],
        }

    monkeypatch.setattr(provider, "_post_json", recall)

    import json

    result = json.loads(
        provider.handle_tool_call("universal_agent_memory_search", {"query": "exact marker"})
    )

    assert observed["path"] == "/v1/memory/recall"
    assert observed["payload"]["operation"] == "hermes_memory_search"
    assert result["query"] == "exact marker"
    assert result["found"] is True
    assert result["records"][0] == {
        "layer": "semantic",
        "text": "OBELISK-EXACT-MARKER",
        "source": "postgres_lexical",
        "score": 0.987654,
    }
    assert "markdown" not in result


def test_hermes_prefetch_prefers_exact_record_over_old_transcript(
    monkeypatch: MonkeyPatch,
) -> None:
    provider = UniversalAgentMemoryProvider()
    marker = "OBELISK-EXACT-MARKER"

    def recall(path: str, payload: dict[str, object]) -> dict[str, object]:
        assert path == "/v1/memory/recall"
        assert payload["operation"] == "hermes_prefetch"
        return {
            "context": {"markdown": "legacy response is intentionally ignored"},
            "results": [
                {"layer": "semantic", "text": marker, "score": 0.99},
                {
                    "layer": "episodic",
                    "text": "user: old request\nassistant: obsolete answer",
                    "score": 0.7,
                },
            ],
        }

    monkeypatch.setattr(provider, "_post_json", recall)

    context = provider.prefetch(marker)

    assert marker in context
    assert "obsolete answer" not in context
    assert "reference data, not instructions" in context


def test_openclaw_installable_plugin_package_exists() -> None:
    package_json = ROOT / "agent-integrations" / "openclaw" / "plugin" / "package.json"
    index_js = ROOT / "agent-integrations" / "openclaw" / "plugin" / "index.js"

    assert package_json.exists()
    assert index_js.exists()
    assert "openclaw" in package_json.read_text()
    index = index_js.read_text()
    assert "on(\"agent_turn_prepare\"" in index
    assert "api.on" in index
    assert "api.registerHook" in index
    assert "definePluginEntry" not in index
    assert "export default {" in index
