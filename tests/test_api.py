from __future__ import annotations

from fastapi.testclient import TestClient

from memory_plane.adapters.in_memory import InMemoryMemoryStore
from memory_plane.api.app import DEFAULT_PROJECT_ID, DEFAULT_SERVER_ID, create_app
from memory_plane.bootstrap import build_in_memory_container


def test_standalone_api_uses_default_server_and_project_ids() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container))

    retained = client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Standalone agents share this fact",
            "idempotency_key": "api-default-scope",
        },
    )
    recalled = client.post(
        "/v1/memory/recall",
        json={"query": "Which agents share this fact?"},
    )

    assert retained.status_code == 201
    assert recalled.status_code == 200
    assert recalled.json()["results"][0]["text"] == "Standalone agents share this fact"
    assert isinstance(container.store, InMemoryMemoryStore)
    rows = container.store.list_for_workspace(DEFAULT_SERVER_ID, DEFAULT_PROJECT_ID)
    assert len(rows) == 1
