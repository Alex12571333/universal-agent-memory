from __future__ import annotations

import base64

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


def test_memory_supersede_endpoint_returns_revision_and_conflict() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    retained = client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Alpha releases on July 15",
        },
    )
    item_id = retained.json()["id"]

    updated = client.put(
        f"/v1/memory/{item_id}/supersede",
        json={
            "text": "Alpha releases on July 16",
            "expected_revision": 1,
            "idempotency_key": "api-supersede-alpha",
        },
    )
    retry = client.put(
        f"/v1/memory/{item_id}/supersede",
        json={
            "text": "Alpha releases on July 16",
            "expected_revision": 1,
            "idempotency_key": "api-supersede-alpha",
        },
    )
    stale = client.put(
        f"/v1/memory/{item_id}/supersede",
        json={"text": "Alpha releases on July 17", "expected_revision": 1},
    )

    assert retained.status_code == 201
    assert retained.json()["revision"] == 1
    assert updated.status_code == 201
    assert updated.json()["revision"] == 2
    assert updated.json()["supersedes_id"] == item_id
    assert retry.status_code == 201
    assert retry.json()["created"] is False
    assert retry.json()["id"] == updated.json()["id"]
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "revision_conflict"
    assert stale.json()["detail"]["actual"] == 2


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


def test_metrics_endpoint_uses_prometheus_text_and_api_key() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container, api_key="secret"))
    client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Metrics count this memory",
        },
        headers={"Authorization": "Bearer secret"},
    )

    denied = client.get("/metrics")
    response = client.get("/metrics", headers={"Authorization": "Bearer secret"})

    assert denied.status_code == 401
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "uam_memory_items_total 1" in response.text
    assert "uam_outbox_pending_total 1" in response.text


def test_api_key_is_disabled_when_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("UAM_API_KEY", raising=False)
    client = TestClient(create_app(build_in_memory_container()))

    response = client.post(
        "/v1/memory/recall",
        json={"query": "No authentication in local mode"},
    )

    assert response.status_code == 200


def test_markdown_document_endpoint_is_idempotent() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    body = {
        "content_base64": base64.b64encode(b"# Decision\n\nUse PostgreSQL.").decode(),
        "format": "markdown",
        "origin_uri": "file:///decision.md",
    }

    first = client.post("/v1/ingest/document", json=body)
    second = client.post("/v1/ingest/document", json=body)

    assert first.status_code == 202
    assert first.json()["created_count"] == 1
    assert second.json()["created_count"] == 0
    assert first.json()["memory_ids"] == second.json()["memory_ids"]


def test_document_endpoint_rejects_invalid_base64() -> None:
    client = TestClient(create_app(build_in_memory_container()))

    response = client.post(
        "/v1/ingest/document",
        json={
            "content_base64": "not base64!",
            "format": "markdown",
            "origin_uri": "file:///invalid.md",
        },
    )

    assert response.status_code == 422


def test_reindex_triggers_embedding_service() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container))

    client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Fact 1",
        },
    )
    client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Fact 2",
        },
    )

    url = f"/v1/workspaces/{DEFAULT_PROJECT_ID}/reindex?tenant_id={DEFAULT_SERVER_ID}"
    response = client.post(url)

    assert response.status_code == 202
    assert response.json() == {"reindexed_count": 2}


def test_conflict_inbox_endpoint_lists_and_persists_review_decision() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Alpha releases on July 15",
        },
    )
    client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Alpha releases on July 16",
        },
    )

    inbox = client.get(f"/v1/workspaces/{DEFAULT_PROJECT_ID}/conflicts")
    case = inbox.json()["cases"][0]
    decision = client.put(
        f"/v1/workspaces/{DEFAULT_PROJECT_ID}/conflicts/{case['id']}/decision",
        json={
            "status": "accepted",
            "winner_value": case["suggested_winner_value"],
            "reason": "newer memory wins",
        },
    )
    unresolved = client.get(f"/v1/workspaces/{DEFAULT_PROJECT_ID}/conflicts")
    resolved = client.get(
        f"/v1/workspaces/{DEFAULT_PROJECT_ID}/conflicts?include_resolved=true"
    )

    assert inbox.status_code == 200
    assert inbox.json()["count"] == 1
    assert case["suggested_winner_value"] == "july 16"
    assert decision.status_code == 200
    assert decision.json()["status"] == "accepted"
    assert unresolved.json()["count"] == 0
    assert resolved.json()["cases"][0]["review_status"] == "accepted"


def test_memory_list_endpoint_and_operator_ui() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    retained = client.post(
        "/v1/memory/retain",
        json={
            "layer": "core",
            "scope": "workspace",
            "kind": "policy",
            "text": "Always preserve append-only evidence.",
            "labels": ["ops"],
        },
    )

    listed = client.get(f"/v1/workspaces/{DEFAULT_PROJECT_ID}/memories?layer=core")
    ui = client.get("/ui")

    assert retained.status_code == 201
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["memories"][0]["text"] == "Always preserve append-only evidence."
    assert ui.status_code == 200
    assert "Универсальная память агентов" in ui.text
    assert "Подробный граф памяти" in ui.text
    assert 'role="tablist"' in ui.text
    assert 'role="navigation"' in ui.text
    assert "Живая карта памяти" in ui.text
    assert "OpenClaw" in ui.text
    assert "Hermes" in ui.text
    assert "Редактируй обычный текст памяти" in ui.text
    assert "Сохранить и пересчитать embedding" in ui.text
    assert "Frontmatter, ревизии и embedding остаются под капотом" in ui.text
    assert "Сервер предлагает самую свежую активную версию" in ui.text
    assert "/v1/workspaces/" in ui.text


def test_retain_endpoint_redacts_secret_before_storage() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container))

    response = client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "log",
            "text": "Leaked key sk-abcdefghijklmnopqrstuvwxyz123456 in trace.",
        },
    )
    rows = container.store.list_for_workspace(DEFAULT_SERVER_ID, DEFAULT_PROJECT_ID)

    assert response.status_code == 201
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in rows[0].text
    assert rows[0].metadata["privacy"]["finding_kinds"] == ["openai_api_key"]


def test_memory_status_filters_list_and_recall() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container))
    client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Rejected memory should stay hidden",
            "status": "rejected",
        },
    )
    client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Active memory should be visible",
        },
    )

    listed = client.get(f"/v1/workspaces/{DEFAULT_PROJECT_ID}/memories?status=rejected")
    recalled = client.post("/v1/memory/recall", json={"query": "memory visible hidden"})

    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["memories"][0]["status"] == "rejected"
    assert "Rejected memory" not in {row["text"] for row in recalled.json()["results"]}


def test_graph_edge_endpoints_create_and_list_neighbors() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    source = client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Graph source",
        },
    ).json()
    target = client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Graph target",
        },
    ).json()

    edge = client.post(
        "/v1/graph/edges",
        json={
            "src_id": source["id"],
            "dst_id": target["id"],
            "edge_type": "supports",
            "weight": 0.9,
        },
    )
    neighbors = client.get(f"/v1/memory/{source['id']}/neighbors")

    assert edge.status_code == 201
    assert edge.json()["edge_type"] == "supports"
    assert neighbors.status_code == 200
    assert neighbors.json()["count"] == 1
    assert neighbors.json()["edges"][0]["dst_id"] == target["id"]


def test_vault_endpoint_exports_markdown_files() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    retained = client.post(
        "/v1/memory/retain",
        json={
            "layer": "core",
            "scope": "workspace",
            "kind": "decision",
            "text": "Universal Agent Memory exposes an Obsidian vault.",
        },
    )

    response = client.get(f"/v1/workspaces/{DEFAULT_PROJECT_ID}/vault")

    assert retained.status_code == 201
    assert response.status_code == 200
    payload = response.json()
    assert payload["file_count"] == 2
    files = {row["path"]: row["content"] for row in payload["files"]}
    assert "README.md" in files
    memory_path = next(path for path in files if path.startswith("core/"))
    assert "type: \"memory\"" in files[memory_path]
    assert "Universal Agent Memory exposes an Obsidian vault." in files[memory_path]


def test_vault_import_endpoint_plans_and_applies_supersede() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container))
    retained = client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Vault import starts as dry run.",
        },
    )
    export = client.get(f"/v1/workspaces/{DEFAULT_PROJECT_ID}/vault")
    files = export.json()["files"]
    memory_file = next(row for row in files if row["path"].startswith("semantic/"))
    memory_file["content"] = memory_file["content"].replace(
        "Vault import starts as dry run.",
        "Vault import can apply through supersede.",
    )

    dry_run = client.post(
        f"/v1/workspaces/{DEFAULT_PROJECT_ID}/vault/import",
        json={"files": [memory_file]},
    )
    applied = client.post(
        f"/v1/workspaces/{DEFAULT_PROJECT_ID}/vault/import",
        json={"dry_run": False, "files": [memory_file]},
    )

    assert retained.status_code == 201
    assert dry_run.status_code == 200
    assert dry_run.json()["dry_run"] is True
    assert dry_run.json()["changes"][0]["action"] == "supersede"
    assert applied.status_code == 200
    assert applied.json()["dry_run"] is False
    assert applied.json()["changes"][0]["action"] == "supersede"
    assert applied.json()["changes"][0]["new_item_id"] is not None
    rows = container.store.list_for_workspace(DEFAULT_SERVER_ID, DEFAULT_PROJECT_ID)
    assert any(row.text == "Vault import can apply through supersede." for row in rows)
