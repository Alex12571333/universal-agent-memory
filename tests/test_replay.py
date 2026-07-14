from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from memory_plane.api.app import create_app
from memory_plane.bootstrap import build_in_memory_container
from memory_plane.contracts.dto import RetainCommand
from memory_plane.domain.models import MemoryLayer, MemoryScope, Provenance


def _retain(container, tenant, workspace, text: str) -> None:
    container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text=text,
            provenance=Provenance(source_kind="test"),
        )
    )


def test_recall_replay_is_durable_redacted_and_workspace_scoped() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container))
    tenant, workspace, other_workspace = uuid4(), uuid4(), uuid4()
    secret_query = "a query that must never be persisted in replay metadata"
    secret_memory = "memory text must not be copied into the replay response"
    _retain(container, tenant, workspace, secret_memory)

    recall = client.post(
        "/v1/memory/recall",
        json={
            "tenant_id": str(tenant),
            "workspace_id": str(workspace),
            "query": secret_query,
            "operation": "operator-review",
            "context_budget_tokens": 512,
        },
    )

    assert recall.status_code == 200
    assert recall.json()["retrieval_traversal"][-1]["stage"] == "fusion"
    replay_id = recall.json()["replay_id"]
    replay = client.get(
        f"/v1/workspaces/{workspace}/replays/{replay_id}",
        params={"tenant_id": str(tenant)},
    )
    audit = client.get(
        "/v1/audit/events",
        params={"tenant_id": str(tenant), "workspace_id": str(workspace)},
    )

    assert replay.status_code == 200
    assert replay.json()["operation"] == "operator-review"
    assert replay.json()["query_chars"] == len(secret_query)
    assert len(replay.json()["query_sha256"]) == 64
    assert replay.json()["references"]
    assert replay.json()["index_freshness"] == {
        "active_memory_count": 1,
        "stale_memory_count": 1,
        "unpublished_memory_count": 1,
        "processing_memory_count": 0,
        "dead_letter_memory_count": 0,
        "missing_delivery_memory_count": 0,
    }
    traversal = replay.json()["retrieval_traversal"]
    assert [step["stage"] for step in traversal] == ["source", "source", "fusion"]
    assert traversal[-1]["name"] == "weighted-fusion"
    assert traversal[-1]["selected_count"] == 1
    assert secret_query not in replay.text
    assert secret_memory not in replay.text
    assert all("text" not in step for step in traversal)
    assert secret_query not in audit.text
    assert any(event["id"] == replay_id for event in audit.json()["events"])
    assert (
        client.get(
            f"/v1/workspaces/{other_workspace}/replays/{replay_id}",
            params={"tenant_id": str(tenant)},
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/v1/workspaces/{workspace}/replays/{replay_id}",
            params={"tenant_id": str(uuid4())},
        ).status_code
        == 404
    )


def test_replay_endpoint_rejects_non_recall_audit_event() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container))
    tenant, workspace = uuid4(), uuid4()
    event = container.audit.record(
        tenant_id=tenant,
        workspace_id=workspace,
        action="vault.import.plan",
        actor="operator",
        actor_type="human",
        resource_type="vault",
    )

    response = client.get(
        f"/v1/workspaces/{workspace}/replays/{event.id}",
        params={"tenant_id": str(tenant)},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "audit event is not a recall replay"
