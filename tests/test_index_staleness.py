from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from memory_plane.api.app import create_app
from memory_plane.bootstrap import build_in_memory_container


def test_recall_marks_index_stale_while_embedding_event_is_pending() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container))

    client.post(
        "/v1/memory/retain",
        json={"layer": "semantic", "scope": "workspace", "kind": "fact", "text": "fresh fact"},
    )
    response = client.post("/v1/memory/recall", json={"query": "fresh fact"})

    assert response.status_code == 200
    assert response.json()["index_stale"] is True


def test_recall_ignores_pending_embedding_in_other_workspace() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container))
    foreign_workspace = uuid4()

    client.post(
        "/v1/memory/retain",
        json={
            "workspace_id": str(foreign_workspace),
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "foreign pending embedding",
        },
    )
    response = client.post("/v1/memory/recall", json={"query": "local workspace"})

    assert response.status_code == 200
    assert response.json()["index_stale"] is False


def test_retrieval_marks_freshness_unknown_as_stale() -> None:
    from memory_plane.contracts.dto import RecallQuery
    container = build_in_memory_container()
    container.retrieval._staleness_check = lambda _query: (_ for _ in ()).throw(RuntimeError())
    result = container.retrieval.recall(
        RecallQuery(tenant_id=uuid4(), workspace_id=uuid4(), text="x")
    )

    assert result.index_stale is True
