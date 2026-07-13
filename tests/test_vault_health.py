from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from memory_plane.api.app import create_app
from memory_plane.bootstrap import build_in_memory_container
from memory_plane.contracts.dto import RetainCommand, SupersedeMemoryCommand
from memory_plane.domain.graph import MemoryEdge, MemoryEdgeType
from memory_plane.domain.models import MemoryLayer, MemoryScope, Provenance


def _retain(container, tenant, workspace, text: str):
    return container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text=text,
            provenance=Provenance(source_kind="test"),
        )
    ).item


def test_vault_health_is_deterministic_and_marks_unlinked_heads_as_warnings() -> None:
    container = build_in_memory_container()
    tenant, workspace = uuid4(), uuid4()
    first = _retain(container, tenant, workspace, "one isolated but valid fact")
    second = _retain(container, tenant, workspace, "a linked fact")
    container.graph.link(
        tenant_id=tenant,
        workspace_id=workspace,
        src_id=first.id,
        dst_id=second.id,
        edge_type=MemoryEdgeType.SUPPORTS,
    )

    report = container.vault_health.inspect(tenant, workspace)

    assert report.healthy is True
    assert report.error_count == 0
    assert report.warning_count == 0
    assert report.edge_count == 1
    assert report.recallable_head_count == 2


def test_vault_health_excludes_superseded_parent_from_unlinked_heads() -> None:
    container = build_in_memory_container()
    tenant, workspace = uuid4(), uuid4()
    original = _retain(container, tenant, workspace, "an old isolated fact")
    container.retention.supersede(
        SupersedeMemoryCommand(
            tenant_id=tenant,
            item_id=original.id,
            replacement_text="the current isolated fact",
            expected_revision=original.revision,
        )
    )

    report = container.vault_health.inspect(tenant, workspace)

    assert report.recallable_head_count == 1
    assert report.unlinked_head_count == 1
    assert [issue.code for issue in report.issues] == ["unlinked_memory_head"]


def test_vault_health_reports_corrupt_graph_edge_without_repairing_it() -> None:
    container = build_in_memory_container()
    tenant, workspace = uuid4(), uuid4()
    item = _retain(container, tenant, workspace, "a valid memory beside corrupt graph data")
    corrupt_edge = MemoryEdge(
        tenant_id=tenant,
        workspace_id=workspace,
        src_id=item.id,
        dst_id=uuid4(),
        edge_type=MemoryEdgeType.SUPPORTS,
    )
    container.store.save_edge(corrupt_edge)

    report = container.vault_health.inspect(tenant, workspace)

    assert report.healthy is False
    assert report.error_count == 1
    assert report.issues[0].code == "broken_graph_endpoint"
    assert report.issues[0].edge_id == corrupt_edge.id
    assert container.store.list_edges_for_workspace(tenant, workspace) == (corrupt_edge,)


def test_vault_health_api_is_workspace_scoped_and_does_not_mutate_memory() -> None:
    container = build_in_memory_container()
    tenant, first_workspace, second_workspace = uuid4(), uuid4(), uuid4()
    _retain(container, tenant, first_workspace, "first workspace fact")
    _retain(container, tenant, second_workspace, "second workspace fact")
    client = TestClient(create_app(container))

    response = client.get(
        f"/v1/workspaces/{first_workspace}/vault/health",
        params={"tenant_id": str(tenant)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace_id"] == str(first_workspace)
    assert payload["memory_count"] == 1
    assert payload["recallable_head_count"] == 1
    assert payload["unlinked_head_count"] == 1
    assert payload["error_count"] == 0
    assert payload["issues"][0]["code"] == "unlinked_memory_head"
    assert len(container.store.list_for_workspace(tenant, first_workspace)) == 1
    assert len(container.store.list_for_workspace(tenant, second_workspace)) == 1
