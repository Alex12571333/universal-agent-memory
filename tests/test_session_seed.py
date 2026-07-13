from __future__ import annotations

from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from memory_plane.api.app import create_app
from memory_plane.bootstrap import build_in_memory_container
from memory_plane.contracts.dto import RetainCommand
from memory_plane.domain.models import MemoryLayer, MemoryScope, MemoryStatus, Provenance


def _retain(
    container,
    tenant,
    workspace,
    *,
    layer,
    scope,
    text,
    status=MemoryStatus.ACTIVE,
    agent_id: UUID | None = None,
):
    return container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=layer,
            scope=scope,
            kind="fact",
            text=text,
            status=status,
            agent_id=agent_id,
            provenance=Provenance(source_kind="test"),
        )
    ).item


def test_session_seed_is_shared_head_only_and_budgeted() -> None:
    container = build_in_memory_container()
    tenant, workspace = uuid4(), uuid4()
    shared = _retain(
        container, tenant, workspace, layer=MemoryLayer.CORE,
        scope=MemoryScope.WORKSPACE, text="Shared production constraint",
        status=MemoryStatus.PINNED,
    )
    _retain(
        container, tenant, workspace, layer=MemoryLayer.CORE,
        scope=MemoryScope.PRIVATE, text="Private agent secret", agent_id=uuid4(),
    )
    _retain(
        container, tenant, workspace, layer=MemoryLayer.SEMANTIC,
        scope=MemoryScope.WORKSPACE, text="Semantic history stays out of the seed",
    )

    seed = container.session_seed.build(tenant, workspace, budget_tokens=128)

    assert seed.trace_ids == (shared.id,)
    assert "Shared production constraint" in seed.markdown
    assert "Private agent secret" not in seed.markdown
    assert "Semantic history" not in seed.markdown
    assert seed.used_tokens <= seed.budget_tokens


def test_session_seed_endpoint_clamps_budget() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container))
    tenant, workspace = uuid4(), uuid4()

    response = client.get(
        f"/v1/workspaces/{workspace}/seed",
        params={"tenant_id": str(tenant), "budget_tokens": 99_999},
    )

    assert response.status_code == 200
    assert response.json()["budget_tokens"] == 4096
