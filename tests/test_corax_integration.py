from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip(
    "agent_core",
    reason="Corax integration tests require the optional agent-core package",
)
pytest.importorskip(
    "agent_sdk",
    reason="Corax integration tests require the optional agent-sdk package",
)

from agent_core import MemoryProvider, MemoryQuery, MemoryRecord, ResultStatus
from agent_sdk import ExtensionManifest, load_extension_instance

ROOT = Path(__file__).resolve().parents[1]
CORAX = ROOT / "agent-integrations" / "corax"
sys.path.insert(0, str(CORAX))

from provider import UniversalAgentMemoryProvider  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.retained: list[dict] = []
        self.recalled: list[dict] = []

    def retain(self, payload: dict) -> dict:
        self.retained.append(payload)
        return {"id": str(uuid4()), "revision": 1, "created": True}

    def recall(self, payload: dict) -> dict:
        self.recalled.append(payload)
        return {"context": {"markdown": "remembered"}, "sources_used": ["postgres"]}

    def health(self) -> bool:
        return True


def test_manifest_loads_memory_provider_contract() -> None:
    manifest = ExtensionManifest.load(CORAX)
    instance = load_extension_instance(
        manifest,
        CORAX,
        kwargs={"client": FakeClient()},
    )
    assert isinstance(instance, MemoryProvider)
    assert manifest.kind.value == "memory_provider"
    assert not manifest.agent_callable


def test_remember_and_recall_preserve_scope() -> None:
    client = FakeClient()
    provider = UniversalAgentMemoryProvider(client=client)
    tenant_id = uuid4()
    workspace_id = uuid4()
    remembered = asyncio.run(
        provider.remember(
            MemoryRecord(
                content="Corax uses typed extensions",
                kind="fact",
                scope={
                    "tenant_id": tenant_id,
                    "workspace_id": workspace_id,
                    "scope": "workspace",
                },
                idempotency_key="corax-1",
            )
        )
    )
    recalled = asyncio.run(
        provider.recall(
            MemoryQuery(
                "typed extensions",
                scopes=(
                    {
                        "tenant_id": tenant_id,
                        "workspace_id": workspace_id,
                    },
                ),
                limit=4,
            )
        )
    )
    assert remembered.status is ResultStatus.SUCCESS
    assert recalled.payload["context"]["markdown"] == "remembered"
    assert client.retained[0]["workspace_id"] == str(workspace_id)
    assert client.recalled[0]["top_k"] == 4


def test_forget_fails_closed() -> None:
    result = asyncio.run(
        UniversalAgentMemoryProvider(client=FakeClient()).forget("memory-1")
    )
    assert result.status is ResultStatus.ERROR
