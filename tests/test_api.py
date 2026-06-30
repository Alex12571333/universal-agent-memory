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


def test_api_key_protects_memory_routes_but_not_health() -> None:
    client = TestClient(create_app(build_in_memory_container(), api_key="secret"))
    body = {
        "layer": "semantic",
        "scope": "workspace",
        "kind": "fact",
        "text": "Protected memory",
    }

    assert client.get("/health").status_code == 200
    missing = client.post("/v1/memory/retain", json=body)
    invalid = client.post(
        "/v1/memory/retain",
        json=body,
        headers={"Authorization": "Bearer wrong"},
    )
    valid = client.post(
        "/v1/memory/retain",
        json=body,
        headers={"Authorization": "Bearer secret"},
    )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert invalid.status_code == 401
    assert valid.status_code == 201


def test_api_key_is_disabled_when_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("UAM_API_KEY", raising=False)
    client = TestClient(create_app(build_in_memory_container()))

    response = client.post(
        "/v1/memory/recall",
        json={"query": "No authentication in local mode"},
    )

    assert response.status_code == 200
