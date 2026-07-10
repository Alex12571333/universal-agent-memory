from __future__ import annotations

import base64
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memory_plane.adapters.in_memory import InMemoryMemoryStore
from memory_plane.api.app import (
    DEFAULT_PROJECT_ID,
    DEFAULT_SERVER_ID,
    DEFAULT_THREAD_ID,
    create_app,
)
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


def test_operator_browser_session_uses_httponly_cookie_and_csrf(monkeypatch) -> None:
    monkeypatch.setenv(
        "UAM_API_KEYS",
        "operator:operator-secret:operator,agent:agent-secret:agent",
    )
    monkeypatch.setenv("UAM_UI_SESSION_SIGNING_KEY", "u" * 64)
    monkeypatch.setenv("UAM_UI_COOKIE_SECURE", "false")
    client = TestClient(create_app(build_in_memory_container()))

    assert client.get("/ui").status_code == 200
    anonymous = client.get("/v1/ui/session")
    denied_agent = client.post("/v1/ui/session", json={"api_key": "agent-secret"})
    login = client.post("/v1/ui/session", json={"api_key": "operator-secret"})

    assert anonymous.json() == {"authenticated": False, "auth_required": True}
    assert denied_agent.status_code == 403
    assert login.status_code == 200
    assert login.json()["principal"] == "operator"
    assert "operator-secret" not in login.text
    cookie = login.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "operator-secret" not in cookie

    assert client.get("/v1/settings/models").status_code == 200
    body = {
        "layer": "semantic",
        "scope": "workspace",
        "kind": "operator_note",
        "text": "browser session protected write",
    }
    missing_csrf = client.post("/v1/memory/retain", json=body)
    accepted = client.post(
        "/v1/memory/retain",
        json=body,
        headers={"X-CSRF-Token": login.json()["csrf_token"]},
    )
    assert missing_csrf.status_code == 403
    assert accepted.status_code == 201

    bad_logout = client.delete("/v1/ui/session")
    logout = client.delete(
        "/v1/ui/session",
        headers={"X-CSRF-Token": login.json()["csrf_token"]},
    )
    assert bad_logout.status_code == 403
    assert logout.status_code == 200
    assert client.get("/v1/settings/models").status_code == 401


def test_browser_session_requires_signing_key_and_secure_cookie_is_configurable(
    monkeypatch,
) -> None:
    monkeypatch.setenv("UAM_API_KEYS", "operator:operator-secret:operator")
    monkeypatch.delenv("UAM_UI_SESSION_SIGNING_KEY", raising=False)
    missing_key = TestClient(create_app(build_in_memory_container()))
    assert missing_key.post(
        "/v1/ui/session",
        json={"api_key": "operator-secret"},
    ).status_code == 503

    monkeypatch.setenv("UAM_UI_SESSION_SIGNING_KEY", "too-short")
    with pytest.raises(ValueError, match="at least 32"):
        create_app(build_in_memory_container())

    monkeypatch.setenv("UAM_UI_SESSION_SIGNING_KEY", "u" * 64)
    monkeypatch.setenv("UAM_UI_COOKIE_SECURE", "true")
    secure = TestClient(create_app(build_in_memory_container()))
    response = secure.post("/v1/ui/session", json={"api_key": "operator-secret"})
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]


def test_api_key_revocation_invalidates_existing_browser_session(monkeypatch) -> None:
    monkeypatch.setenv("UAM_API_KEYS", "operator:operator-secret:operator")
    monkeypatch.setenv("UAM_UI_SESSION_SIGNING_KEY", "u" * 64)
    client = TestClient(create_app(build_in_memory_container()))
    login = client.post("/v1/ui/session", json={"api_key": "operator-secret"})
    csrf = login.json()["csrf_token"]
    listed = client.get("/v1/keys")
    operator_key = listed.json()["keys"][0]

    revoked = client.post(
        f"/v1/keys/{operator_key['id']}/revoke",
        json={"reason": "browser-session revocation test"},
        headers={"X-CSRF-Token": csrf},
    )
    denied = client.get("/v1/settings/models")

    assert revoked.status_code == 200
    assert denied.status_code == 401


def test_readiness_is_public_and_reports_optional_degradation(monkeypatch) -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container, api_key="secret"))

    healthy = client.get("/ready")
    container.retrieval.record_failure("qdrant_hybrid", ConnectionError("offline"))
    degraded = client.get("/ready")
    monkeypatch.setattr(container.store, "ping", lambda: False)
    failed = client.get("/ready")

    assert healthy.status_code == 200
    assert healthy.json()["status"] == "ready"
    assert degraded.status_code == 200
    assert degraded.json()["status"] == "degraded"
    assert degraded.json()["retrieval_sources"]["qdrant_hybrid"]["error_type"] == (
        "ConnectionError"
    )
    metrics = client.get("/metrics", headers={"Authorization": "Bearer secret"})
    assert "uam_retrieval_source_failures_total 1" in metrics.text
    assert "uam_retrieval_degraded_sources 1" in metrics.text
    assert failed.status_code == 503
    assert failed.json()["status"] == "not_ready"


def test_api_responses_include_security_headers() -> None:
    client = TestClient(create_app(build_in_memory_container(), api_key="secret"))

    public = client.get("/health")
    denied = client.post("/v1/memory/recall", json={"query": "protected"})
    allowed = client.post(
        "/v1/memory/recall",
        json={"query": "protected"},
        headers={"Authorization": "Bearer secret"},
    )

    for response in (public, denied, allowed):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "camera=()" in response.headers["permissions-policy"]


def test_scoped_api_keys_limit_agent_and_operator_access(monkeypatch) -> None:
    monkeypatch.setenv(
        "UAM_API_KEYS",
        "reader:read-secret:read,agent:agent-secret:agent,operator:operator-secret:operator",
    )
    client = TestClient(create_app(build_in_memory_container()))
    body = {
        "layer": "semantic",
        "scope": "workspace",
        "kind": "fact",
        "text": "Scoped key memory",
    }

    read_recall = client.post(
        "/v1/memory/recall",
        json={"query": "Scoped"},
        headers={"Authorization": "Bearer read-secret"},
    )
    read_write = client.post(
        "/v1/memory/retain",
        json=body,
        headers={"Authorization": "Bearer read-secret"},
    )
    agent_write = client.post(
        "/v1/memory/retain",
        json=body,
        headers={"Authorization": "Bearer agent-secret"},
    )
    agent_metrics = client.get("/metrics", headers={"Authorization": "Bearer agent-secret"})
    operator_metrics = client.get("/metrics", headers={"Authorization": "Bearer operator-secret"})

    assert read_recall.status_code == 200
    assert read_write.status_code == 403
    assert agent_write.status_code == 201
    assert agent_metrics.status_code == 403
    assert operator_metrics.status_code == 200


def test_identity_provisioning_is_operator_only_idempotent_and_scope_safe(monkeypatch) -> None:
    monkeypatch.setenv(
        "UAM_API_KEYS",
        "agent:agent-secret:agent,operator:operator-secret:operator",
    )
    client = TestClient(create_app(build_in_memory_container()))
    agent_id = uuid4()
    thread_id = uuid4()
    body = {
        "agent_id": str(agent_id),
        "agent_name": "OpenClaw primary",
        "agent_role": "openclaw",
        "agent_config": {"namespace": "openclaw/default"},
        "thread_id": str(thread_id),
    }

    denied = client.post(
        "/v1/identities/provision",
        json=body,
        headers={"Authorization": "Bearer agent-secret"},
    )
    created = client.post(
        "/v1/identities/provision",
        json=body,
        headers={"Authorization": "Bearer operator-secret"},
    )
    updated = client.post(
        "/v1/identities/provision",
        json={**body, "agent_name": "OpenClaw production"},
        headers={"Authorization": "Bearer operator-secret"},
    )
    collision = client.post(
        "/v1/identities/provision",
        json={**body, "workspace_id": str(uuid4())},
        headers={"Authorization": "Bearer operator-secret"},
    )

    assert denied.status_code == 403
    assert created.status_code == 200
    assert created.json()["thread"]["owner_agent_id"] == str(agent_id)
    assert updated.status_code == 200
    assert updated.json()["agent"]["name"] == "OpenClaw production"
    assert collision.status_code == 409


def test_bound_agent_keys_enforce_identity_private_memory_and_thread_ownership(
    monkeypatch,
) -> None:
    agent_a = uuid4()
    agent_b = uuid4()
    thread_a = uuid4()
    thread_b = uuid4()
    monkeypatch.setenv(
        "UAM_API_KEYS",
        "agent-a:key-a:agent,agent-b:key-b:agent,operator:operator-key:operator",
    )
    monkeypatch.setenv(
        "UAM_API_PRINCIPAL_BINDINGS_JSON",
        json.dumps(
            {
                "agent-a": {
                    "tenant_id": str(DEFAULT_SERVER_ID),
                    "workspace_id": str(DEFAULT_PROJECT_ID),
                    "agent_id": str(agent_a),
                },
                "agent-b": {
                    "tenant_id": str(DEFAULT_SERVER_ID),
                    "workspace_id": str(DEFAULT_PROJECT_ID),
                    "agent_id": str(agent_b),
                },
            }
        ),
    )
    monkeypatch.setenv("UAM_REQUIRE_IDENTITY_BINDINGS", "true")
    client = TestClient(create_app(build_in_memory_container()))
    operator = {"Authorization": "Bearer operator-key"}
    headers_a = {"Authorization": "Bearer key-a"}
    headers_b = {"Authorization": "Bearer key-b"}
    for agent_id, thread_id, name in (
        (agent_a, thread_a, "Agent A"),
        (agent_b, thread_b, "Agent B"),
    ):
        provisioned = client.post(
            "/v1/identities/provision",
            headers=operator,
            json={
                "agent_id": str(agent_id),
                "agent_name": name,
                "agent_role": "test-agent",
                "thread_id": str(thread_id),
            },
        )
        assert provisioned.status_code == 200

    def retain_private(agent_id, text, headers):  # type: ignore[no-untyped-def]
        return client.post(
            "/v1/memory/retain",
            headers=headers,
            json={
                "tenant_id": str(DEFAULT_SERVER_ID),
                "workspace_id": str(DEFAULT_PROJECT_ID),
                "agent_id": str(agent_id),
                "layer": "semantic",
                "scope": "private",
                "kind": "preference",
                "text": text,
            },
        )

    assert retain_private(agent_a, "Agent A private marker", headers_a).status_code == 201
    assert retain_private(agent_b, "Agent B private marker", headers_b).status_code == 201
    recalled_a = client.post(
        "/v1/memory/recall",
        headers=headers_a,
        json={
            "tenant_id": str(DEFAULT_SERVER_ID),
            "workspace_id": str(DEFAULT_PROJECT_ID),
            "agent_id": str(agent_a),
            "query": "private marker",
            "top_k": 20,
        },
    )
    forged_agent = retain_private(agent_b, "forged", headers_a)
    forged_workspace = client.post(
        "/v1/memory/recall",
        headers=headers_a,
        json={
            "tenant_id": str(DEFAULT_SERVER_ID),
            "workspace_id": str(uuid4()),
            "agent_id": str(agent_a),
            "query": "anything",
        },
    )
    foreign_thread = client.get(
        f"/v1/checkpoints/{thread_b}?tenant_id={DEFAULT_SERVER_ID}",
        headers=headers_a,
    )
    operator_route = client.get(
        f"/v1/workspaces/{DEFAULT_PROJECT_ID}/conflicts",
        headers=headers_a,
    )

    assert recalled_a.status_code == 200
    assert [row["text"] for row in recalled_a.json()["results"]] == [
        "Agent A private marker"
    ]
    assert forged_agent.status_code == 403
    assert forged_workspace.status_code == 403
    assert foreign_thread.status_code == 403
    assert operator_route.status_code == 403


def test_strict_binding_mode_rejects_unbound_agent_key(monkeypatch) -> None:
    monkeypatch.setenv("UAM_API_KEYS", "agent:agent-secret:agent")
    monkeypatch.setenv("UAM_REQUIRE_IDENTITY_BINDINGS", "true")
    monkeypatch.delenv("UAM_API_PRINCIPAL_BINDINGS_JSON", raising=False)

    with pytest.raises(RuntimeError, match="require identity bindings"):
        create_app(build_in_memory_container())


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/v1/graph"),
        ("GET", f"/v1/workspaces/{DEFAULT_PROJECT_ID}/memories"),
        ("GET", f"/v1/memory/{uuid4()}/neighbors"),
        ("POST", f"/v1/memory/{uuid4()}/supersede"),
        ("GET", "/v1/memory/proposals"),
        ("POST", f"/v1/memory/proposals/{uuid4()}/accept"),
        ("GET", "/v1/conversations"),
        ("POST", "/v1/conversations/curate"),
        ("GET", "/v1/checkpoints"),
        ("POST", f"/v1/checkpoints/{uuid4()}/compact"),
        ("GET", "/v1/audit/events"),
        ("GET", "/v1/settings/models"),
    ),
)
def test_bound_agent_key_cannot_access_operator_control_plane(
    monkeypatch,
    method: str,
    path: str,
) -> None:
    agent_id = uuid4()
    monkeypatch.setenv("UAM_API_KEYS", "agent:agent-secret:agent")
    monkeypatch.setenv(
        "UAM_API_PRINCIPAL_BINDINGS_JSON",
        json.dumps(
            {
                "agent": {
                    "tenant_id": str(DEFAULT_SERVER_ID),
                    "workspace_id": str(DEFAULT_PROJECT_ID),
                    "agent_id": str(agent_id),
                }
            }
        ),
    )
    monkeypatch.setenv("UAM_REQUIRE_IDENTITY_BINDINGS", "true")
    client = TestClient(create_app(build_in_memory_container()))

    response = client.request(
        method,
        path,
        headers={"Authorization": "Bearer agent-secret"},
        json={} if method == "POST" else None,
    )

    assert response.status_code == 403
    assert response.json()["required_scope"] == "operator"


def test_api_auth_reads_master_and_scoped_keys_from_files(monkeypatch, tmp_path) -> None:
    master_file = tmp_path / "master"
    scoped_file = tmp_path / "scoped"
    master_file.write_text("master-secret\n", encoding="utf-8")
    scoped_file.write_text(
        "agent:agent-secret:agent,operator:operator-secret:operator\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("UAM_API_KEY", raising=False)
    monkeypatch.delenv("UAM_API_KEYS", raising=False)
    monkeypatch.setenv("UAM_API_KEY_FILE", str(master_file))
    monkeypatch.setenv("UAM_API_KEYS_FILE", str(scoped_file))
    client = TestClient(create_app(build_in_memory_container()))

    denied = client.get("/metrics", headers={"Authorization": "Bearer agent-secret"})
    scoped_allowed = client.get("/metrics", headers={"Authorization": "Bearer operator-secret"})
    master_allowed = client.get("/metrics", headers={"Authorization": "Bearer master-secret"})

    assert denied.status_code == 403
    assert scoped_allowed.status_code == 200
    assert master_allowed.status_code == 200


def test_audit_events_require_operator_scope(monkeypatch) -> None:
    monkeypatch.setenv(
        "UAM_API_KEYS",
        "agent:agent-secret:agent,operator:operator-secret:operator",
    )
    client = TestClient(create_app(build_in_memory_container()))

    retained = client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Audit scope test memory",
        },
        headers={"Authorization": "Bearer agent-secret"},
    )
    denied = client.get(
        "/v1/audit/events",
        headers={"Authorization": "Bearer agent-secret"},
    )
    allowed = client.get(
        "/v1/audit/events",
        headers={"Authorization": "Bearer operator-secret"},
    )

    assert retained.status_code == 201
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["count"] == 1
    event = allowed.json()["events"][0]
    assert event["action"] == "memory.retain"
    assert event["actor"] == "agent"
    assert event["actor_type"] == "agent"


def test_api_key_registry_tracks_last_used_and_revocation(monkeypatch) -> None:
    monkeypatch.setenv(
        "UAM_API_KEYS",
        "agent:agent-secret:agent,operator:operator-secret:operator",
    )
    client = TestClient(create_app(build_in_memory_container()))

    agent_write = client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Key registry tracks this agent call.",
        },
        headers={"Authorization": "Bearer agent-secret"},
    )
    listed = client.get(
        "/v1/keys",
        headers={"Authorization": "Bearer operator-secret"},
    )
    agent_key = next(row for row in listed.json()["keys"] if row["name"] == "agent")
    operator_key = next(
        row for row in listed.json()["keys"] if row["name"] == "operator"
    )
    revoked = client.post(
        f"/v1/keys/{agent_key['id']}/revoke",
        json={"reason": "rotation drill"},
        headers={"Authorization": "Bearer operator-secret"},
    )
    denied_after_revoke = client.post(
        "/v1/memory/recall",
        json={"query": "Key registry"},
        headers={"Authorization": "Bearer agent-secret"},
    )
    audit = client.get(
        "/v1/audit/events?action=api_key.revoke",
        headers={"Authorization": "Bearer operator-secret"},
    )
    relisted = client.get(
        "/v1/keys",
        headers={"Authorization": "Bearer operator-secret"},
    )
    revoked_key = next(row for row in relisted.json()["keys"] if row["name"] == "agent")

    assert agent_write.status_code == 201
    assert listed.status_code == 200
    assert listed.json()["count"] == 2
    assert agent_key["last_used_at"] is not None
    assert operator_key["last_used_at"] is not None
    assert "agent-secret" not in str(listed.json())
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert revoked.json()["revoked_reason"] == "rotation drill"
    assert denied_after_revoke.status_code == 401
    assert denied_after_revoke.json()["detail"] == "API key has been revoked"
    assert audit.status_code == 200
    assert audit.json()["count"] == 1
    assert audit.json()["events"][0]["metadata"]["name"] == "agent"
    assert revoked_key["revoked"] is True


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
    assert "uam_audit_events_total 1" in response.text
    assert "uam_api_keys_total 1" in response.text
    assert "uam_embedding_operations_total 0" in response.text
    assert "uam_embedding_failures_total 0" in response.text


def test_api_key_is_disabled_when_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("UAM_API_KEY", raising=False)
    client = TestClient(create_app(build_in_memory_container()))

    response = client.post(
        "/v1/memory/recall",
        json={"query": "No authentication in local mode"},
    )

    assert response.status_code == 200


def test_recall_default_context_budget_is_128k() -> None:
    client = TestClient(create_app(build_in_memory_container()))

    response = client.post(
        "/v1/memory/recall",
        json={"query": "production context budget"},
    )

    assert response.status_code == 200
    assert response.json()["context"]["budget_tokens"] == 131072


def test_recall_128k_context_can_include_more_than_100_items() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    for index in range(120):
        retained = client.post(
            "/v1/memory/retain",
            json={
                "layer": "semantic",
                "scope": "workspace",
                "kind": "fact",
                "text": f"Bulk 128k context memory {index} shared keyword zephyr.",
                "idempotency_key": f"api-bulk-128k:{index}",
            },
        )
        assert retained.status_code == 201

    response = client.post(
        "/v1/memory/recall",
        json={"query": "zephyr", "top_k": 120},
    )

    assert response.status_code == 200
    assert response.json()["context"]["budget_tokens"] == 131072
    assert len(response.json()["context"]["trace_ids"]) == 120


def test_conversation_turn_endpoint_stores_raw_transcript_separately() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container))
    body = {
        "namespace": "hermes",
        "thread_id": str(DEFAULT_THREAD_ID),
        "source_kind": "test-suite",
        "messages": [
            {"role": "user", "content": "Запомни весь этот диалог"},
            {"role": "assistant", "content": "Ок, пишу transcript turn"},
        ],
        "idempotency_key": "turn-1",
    }

    first = client.post("/v1/conversations/turns", json=body)
    retry = client.post("/v1/conversations/turns", json=body)
    listed = client.get("/v1/conversations/turns?namespace=hermes")
    recalled = client.post(
        "/v1/memory/recall",
        json={"query": "Запомни весь этот диалог"},
    )

    assert first.status_code == 201
    assert first.json()["created"] is True
    assert first.json()["retention_policy"] == "raw_and_curated"
    assert first.json()["messages"][0]["content"] == "Запомни весь этот диалог"
    assert retry.status_code == 201
    assert retry.json()["created"] is False
    assert retry.json()["id"] == first.json()["id"]
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["turns"][0]["namespace"] == "hermes"
    assert recalled.status_code == 200
    assert recalled.json()["results"] == []


def test_conversation_turn_endpoint_applies_privacy_guard() -> None:
    client = TestClient(create_app(build_in_memory_container()))

    response = client.post(
        "/v1/conversations/turns",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": "password=supersecret123 надо убрать",
                }
            ],
        },
    )

    assert response.status_code == 201
    message = response.json()["messages"][0]
    assert "supersecret123" not in message["content"]
    assert "[REDACTED:password_assignment]" in message["content"]
    assert message["metadata"]["privacy"]["finding_count"] == 1


def test_conversation_curate_endpoint_creates_recallable_memory() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    turn = client.post(
        "/v1/conversations/turns",
        json={
            "namespace": "openclaw",
            "thread_id": str(DEFAULT_THREAD_ID),
            "messages": [
                {"role": "user", "content": "Интерфейс должен быть на русском"},
                {"role": "assistant", "content": "Принял, буду делать русский UI"},
            ],
        },
    )

    curated = client.post(
        f"/v1/conversations/turns/{turn.json()['id']}/curate",
        json={"labels": ["ui"], "idempotency_key": "curate-russian-ui"},
    )
    retry = client.post(
        f"/v1/conversations/turns/{turn.json()['id']}/curate",
        json={"labels": ["ui"], "idempotency_key": "curate-russian-ui"},
    )
    recalled = client.post(
        "/v1/memory/recall",
        json={
            "query": "русский интерфейс",
            "thread_id": str(DEFAULT_THREAD_ID),
        },
    )

    assert curated.status_code == 201
    assert curated.json()["created"] is True
    assert retry.status_code == 201
    assert retry.json()["created"] is False
    assert retry.json()["id"] == curated.json()["id"]
    assert recalled.status_code == 200
    assert recalled.json()["results"][0]["id"] == curated.json()["id"]
    assert "Conversation turn summary" in recalled.json()["results"][0]["text"]
    assert "Интерфейс должен быть на русском" in recalled.json()["results"][0]["text"]


def test_memory_proposal_endpoint_stores_review_item_not_recall_memory() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    body = {
        "namespace": "openclaw",
        "requester": "openclaw-plugin",
        "target": "preference",
        "proposal": "User prefers the interface in Russian.",
        "evidence": "User complained that the UI was not in Russian.",
        "confidence": 0.91,
        "importance": 0.8,
        "idempotency_key": "proposal-russian-ui",
    }

    first = client.post("/v1/memory/proposals", json=body)
    retry = client.post("/v1/memory/proposals", json=body)
    listed = client.get("/v1/memory/proposals?namespace=openclaw&status=open")
    recalled = client.post(
        "/v1/memory/recall",
        json={"query": "Russian interface"},
    )

    assert first.status_code == 201
    assert first.json()["created"] is True
    assert first.json()["status"] == "open"
    assert first.json()["target"] == "preference"
    assert retry.status_code == 201
    assert retry.json()["created"] is False
    assert retry.json()["id"] == first.json()["id"]
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["proposals"][0]["requester"] == "openclaw-plugin"
    assert recalled.status_code == 200
    assert recalled.json()["results"] == []


def test_memory_proposal_endpoint_applies_privacy_guard() -> None:
    client = TestClient(create_app(build_in_memory_container()))

    response = client.post(
        "/v1/memory/proposals",
        json={
            "proposal": "Remember password=supersecret123 as the deploy password",
            "evidence": "Operator pasted password=supersecret123",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert "supersecret123" not in payload["proposal"]
    assert "supersecret123" not in payload["evidence"]
    assert "[REDACTED:password_assignment]" in payload["proposal"]
    assert payload["metadata"]["privacy"]["finding_count"] == 2


def test_memory_proposal_accept_creates_recallable_memory() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    proposal = client.post(
        "/v1/memory/proposals",
        json={
            "namespace": "hermes",
            "target": "preference",
            "requester": "hermes-memory-provider",
            "proposal": "User wants premium Russian interface polish.",
            "evidence": "User asked for a more premium Russian dashboard.",
            "confidence": 0.9,
            "importance": 0.8,
        },
    )

    accepted = client.post(
        f"/v1/memory/proposals/{proposal.json()['id']}/accept",
        json={
            "reviewer": "operator",
            "reason": "Evidence is explicit.",
            "idempotency_key": "accept-premium-russian-ui",
        },
    )
    retry = client.post(
        f"/v1/memory/proposals/{proposal.json()['id']}/accept",
        json={
            "reviewer": "operator",
            "reason": "Retry should be idempotent.",
            "idempotency_key": "accept-premium-russian-ui",
        },
    )
    recalled = client.post(
        "/v1/memory/recall",
        json={"query": "premium Russian interface"},
    )

    assert accepted.status_code == 201
    assert accepted.json()["proposal"]["status"] == "accepted"
    assert accepted.json()["proposal"]["metadata"]["accepted_memory_id"]
    memory = accepted.json()["memory"]
    assert memory["created"] is True
    assert retry.status_code == 201
    assert retry.json()["memory"]["created"] is False
    assert retry.json()["memory"]["id"] == memory["id"]
    assert recalled.status_code == 200
    assert recalled.json()["results"][0]["id"] == memory["id"]
    assert recalled.json()["results"][0]["layer"] == "social"


def test_memory_proposal_reject_does_not_create_memory_and_blocks_accept() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    proposal = client.post(
        "/v1/memory/proposals",
        json={
            "target": "fact",
            "proposal": "Probably user likes orange buttons.",
            "evidence": "No direct evidence.",
        },
    )

    rejected = client.post(
        f"/v1/memory/proposals/{proposal.json()['id']}/reject",
        json={"reviewer": "operator", "reason": "Weak evidence."},
    )
    accepted = client.post(
        f"/v1/memory/proposals/{proposal.json()['id']}/accept",
        json={"reviewer": "operator"},
    )
    recalled = client.post(
        "/v1/memory/recall",
        json={"query": "orange buttons"},
    )

    assert rejected.status_code == 200
    assert rejected.json()["proposal"]["status"] == "rejected"
    assert rejected.json()["memory"] is None
    assert accepted.status_code == 422
    assert "rejected proposal cannot be accepted" in accepted.text
    assert recalled.status_code == 200
    assert recalled.json()["results"] == []


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
    assert decision.json()["applied_memory_id"] is not None
    assert unresolved.json()["count"] == 0
    assert resolved.json()["cases"][0]["review_status"] == "accepted"
    recalled = client.post(
        "/v1/memory/recall",
        json={"query": "Alpha release July", "top_k": 10},
    )
    assert [row["text"] for row in recalled.json()["results"]] == [
        "Alpha releases on July 16"
    ]


def test_conflict_decision_can_dismiss_without_winner() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Dismissable releases on July 15",
        },
    )
    client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Dismissable releases on July 16",
        },
    )
    case = client.get(f"/v1/workspaces/{DEFAULT_PROJECT_ID}/conflicts").json()[
        "cases"
    ][0]

    decision = client.put(
        f"/v1/workspaces/{DEFAULT_PROJECT_ID}/conflicts/{case['id']}/decision",
        json={
            "status": "dismissed",
            "winner_value": None,
            "reason": "Not actionable for this workspace.",
        },
    )
    unresolved = client.get(f"/v1/workspaces/{DEFAULT_PROJECT_ID}/conflicts")
    resolved = client.get(
        f"/v1/workspaces/{DEFAULT_PROJECT_ID}/conflicts?include_resolved=true"
    )

    assert decision.status_code == 200
    assert decision.json()["status"] == "dismissed"
    assert decision.json()["winner_value"] is None
    assert unresolved.json()["count"] == 0
    assert resolved.json()["cases"][0]["review_status"] == "dismissed"


def test_memory_list_endpoint_and_operator_ui(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UAM_WEB_DIST", str(tmp_path / "missing-dist"))
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
    assert "Настройки моделей" in ui.text
    assert 'value="openai-compatible"' in ui.text
    assert "OpenAI-compatible gateway" in ui.text
    assert "Obsidian‑style карта" in ui.text
    assert "mountForceGraph" in ui.text
    assert "/v1/settings/models" in ui.text
    assert "Редактируй обычный текст памяти" in ui.text
    assert "Сохранить и пересчитать embedding" in ui.text
    assert "Frontmatter, ревизии и embedding остаются под капотом" in ui.text
    assert "Сервер предлагает самую свежую активную версию" in ui.text
    assert "Принять рекомендацию" in ui.text
    assert "Скрыть как неактуальный" in ui.text
    assert "decideConflict(" in ui.text
    assert "/v1/workspaces/" in ui.text


def test_operator_ui_serves_react_dist_when_built(tmp_path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<div id="root"></div><script type="module" src="/ui/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text('console.log("react-dashboard")', encoding="utf-8")
    monkeypatch.setenv("UAM_WEB_DIST", str(dist))

    client = TestClient(create_app(build_in_memory_container()))

    ui = client.get("/ui")
    asset = client.get("/ui/assets/app.js")
    nested = client.get("/ui/settings")

    assert ui.status_code == 200
    assert '<div id="root"></div>' in ui.text
    assert asset.status_code == 200
    assert "react-dashboard" in asset.text
    assert nested.status_code == 200
    assert '<div id="root"></div>' in nested.text


def test_model_settings_endpoints_save_and_probe_fake_provider() -> None:
    client = TestClient(create_app(build_in_memory_container()))

    current = client.get("/v1/settings/models")
    saved = client.put(
        "/v1/settings/models",
        json={
            "provider": "fake",
            "model_name": "fake-ui-test",
            "dimension": 32,
            "base_url": None,
            "api_key": "local-secret",
            "timeout_seconds": 5,
        },
    )
    probed = client.post(
        "/v1/settings/models/test",
        json={
            "provider": "fake",
            "model_name": "fake-ui-test",
            "dimension": 32,
            "timeout_seconds": 5,
        },
    )
    resaved = client.put(
        "/v1/settings/models",
        json={
            "provider": "fake",
            "model_name": "fake-ui-test-2",
            "dimension": 32,
            "base_url": None,
            "api_key": None,
            "timeout_seconds": 5,
        },
    )

    assert current.status_code == 200
    assert current.json()["runtime"]["model_name"] == "fake-embed-v1"
    assert current.json()["restart_required"] is False
    assert saved.status_code == 200
    assert saved.json()["desired"]["model_name"] == "fake-ui-test"
    assert saved.json()["desired"]["api_key"] == "loca…cret"
    assert saved.json()["env"]["UAM_EMBEDDING_MODEL"] == "fake-ui-test"
    assert saved.json()["env"]["UAM_EMBEDDING_SEND_DIMENSIONS"] == "false"
    assert saved.json()["restart_required"] is True
    assert probed.status_code == 200
    assert probed.json()["ok"] is True
    assert probed.json()["dimension"] == 32
    assert resaved.status_code == 200
    assert resaved.json()["desired"]["api_key"] == "loca…cret"


def test_model_settings_enforce_endpoint_allowlist_and_never_persist_api_key(
    monkeypatch,
    tmp_path,
) -> None:
    settings_path = tmp_path / "model-settings.json"
    monkeypatch.setenv("UAM_MODEL_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("UAM_MODEL_ENDPOINT_ALLOWLIST", "https://allowed.example")
    client = TestClient(create_app(build_in_memory_container()))
    body = {
        "provider": "openai-compatible",
        "model_name": "provider/model-v2",
        "dimension": 32,
        "base_url": "https://allowed.example/v1",
        "api_key": "provider-secret-value",
        "timeout_seconds": 5,
    }

    blocked_save = client.put(
        "/v1/settings/models",
        json={**body, "base_url": "http://169.254.169.254/latest/meta-data"},
    )
    blocked_probe = client.post(
        "/v1/settings/models/test",
        json={**body, "base_url": "https://not-allowed.example/v1"},
    )
    saved = client.put("/v1/settings/models", json=body)

    assert blocked_save.status_code == 422
    assert blocked_probe.status_code == 422
    assert "allowlist" in blocked_probe.json()["detail"]
    assert saved.status_code == 200
    assert saved.json()["desired"]["api_key"] == "prov…alue"
    persisted = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "api_key" not in persisted
    assert "provider-secret-value" not in settings_path.read_text(encoding="utf-8")
    assert settings_path.stat().st_mode & 0o777 == 0o600


def test_loading_legacy_model_settings_removes_persisted_api_key(monkeypatch, tmp_path) -> None:
    settings_path = tmp_path / "model-settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "provider": "fake",
                "model_name": "legacy",
                "dimension": 32,
                "base_url": None,
                "api_key": "legacy-plaintext-secret",
                "timeout_seconds": 5,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("UAM_MODEL_SETTINGS_PATH", str(settings_path))

    response = TestClient(create_app(build_in_memory_container())).get(
        "/v1/settings/models"
    )

    assert response.status_code == 200
    assert response.json()["desired"]["api_key"] is None
    assert "legacy-plaintext-secret" not in settings_path.read_text(encoding="utf-8")


def test_system_status_endpoint_reports_real_process_fields(monkeypatch) -> None:
    expected_build = {
        "version": "1.2.3",
        "source_commit": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "deployment_id": "api-status-test",
        "build_time": "2026-07-10T00:00:00Z",
    }
    for field, value in expected_build.items():
        monkeypatch.setenv(f"UAM_{field.upper()}", value)
    client = TestClient(create_app(build_in_memory_container()))

    response = client.get("/v1/system/status")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == expected_build["version"]
    assert data["build"] == expected_build
    assert data["uptime_seconds"] >= 0
    assert data["storage"]["total_bytes"] > 0
    assert data["storage"]["used_bytes"] > 0
    assert data["process"]["pid"] > 0
    assert "one_minute" in data["load_average"]


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
            "text": "Obelisk Memory exposes an Obsidian vault.",
        },
    )

    response = client.get(f"/v1/workspaces/{DEFAULT_PROJECT_ID}/vault")

    assert retained.status_code == 201
    assert response.status_code == 200
    payload = response.json()
    assert payload["file_count"] == 2
    files = {row["path"]: row["content"] for row in payload["files"]}
    editable_files = {row["path"]: row["editable_content"] for row in payload["files"]}
    assert "README.md" in files
    memory_path = next(path for path in files if path.startswith("core/"))
    assert "type: \"memory\"" in files[memory_path]
    assert "Obelisk Memory exposes an Obsidian vault." in files[memory_path]
    assert editable_files[memory_path] == "Obelisk Memory exposes an Obsidian vault."
    assert "Provenance" not in editable_files[memory_path]
    assert "tenant_id" not in editable_files[memory_path]


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


def test_vault_archive_endpoint_hides_memory_from_recall() -> None:
    container = build_in_memory_container()
    client = TestClient(create_app(container))
    retained = client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Temporary UI delete test memory.",
        },
    )
    export = client.get(f"/v1/workspaces/{DEFAULT_PROJECT_ID}/vault")
    memory_file = next(
        row for row in export.json()["files"] if row["path"].startswith("semantic/")
    )

    archived = client.post(
        f"/v1/workspaces/{DEFAULT_PROJECT_ID}/vault/archive",
        json={"file": memory_file},
    )
    recall = client.post(
        "/v1/memory/recall",
        json={
            "query": "Temporary UI delete test memory",
            "workspace_id": str(DEFAULT_PROJECT_ID),
        },
    )

    assert retained.status_code == 201
    assert archived.status_code == 200
    assert archived.json()["changes"][0]["action"] == "archive"
    assert archived.json()["changes"][0]["new_item_id"] is not None
    assert recall.status_code == 200
    assert recall.json()["results"] == []
    rows = container.store.list_for_workspace(DEFAULT_SERVER_ID, DEFAULT_PROJECT_ID)
    assert any(row.status.value == "archived" for row in rows)


def test_audit_trail_records_operator_memory_and_vault_actions() -> None:
    client = TestClient(create_app(build_in_memory_container()))
    retained = client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Audit trail starts with retain.",
        },
    )
    superseded = client.put(
        f"/v1/memory/{retained.json()['id']}/supersede",
        json={
            "text": "Audit trail includes supersede.",
            "expected_revision": 1,
        },
    )
    client.put(
        "/v1/settings/models",
        json={
            "provider": "fake",
            "model_name": "fake-audit-test",
            "dimension": 32,
            "timeout_seconds": 5,
        },
    )
    client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "AuditConflict releases on July 15",
        },
    )
    client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "AuditConflict releases on July 16",
        },
    )
    conflict = client.get(f"/v1/workspaces/{DEFAULT_PROJECT_ID}/conflicts").json()[
        "cases"
    ][0]
    decided = client.put(
        f"/v1/workspaces/{DEFAULT_PROJECT_ID}/conflicts/{conflict['id']}/decision",
        json={
            "status": "accepted",
            "winner_value": conflict["suggested_winner_value"],
            "reason": "audit trail test",
        },
    )
    export = client.get(f"/v1/workspaces/{DEFAULT_PROJECT_ID}/vault")
    memory_file = next(
        row for row in export.json()["files"] if row["path"].startswith("semantic/")
    )
    planned = client.post(
        f"/v1/workspaces/{DEFAULT_PROJECT_ID}/vault/import",
        json={"files": [memory_file]},
    )
    archived = client.post(
        f"/v1/workspaces/{DEFAULT_PROJECT_ID}/vault/archive",
        json={"file": memory_file},
    )

    audit = client.get(
        f"/v1/audit/events?workspace_id={DEFAULT_PROJECT_ID}&limit=50"
    )
    memory_audit = client.get(
        "/v1/audit/events?action=memory.supersede&resource_type=memory_item"
    )

    assert retained.status_code == 201
    assert superseded.status_code == 201
    assert decided.status_code == 200
    assert planned.status_code == 200
    assert archived.status_code == 200
    assert audit.status_code == 200
    actions = {event["action"] for event in audit.json()["events"]}
    assert {
        "memory.retain",
        "memory.supersede",
        "settings.models.save",
        "conflict.decide",
        "vault.import.plan",
        "vault.archive",
    }.issubset(actions)
    assert memory_audit.status_code == 200
    assert memory_audit.json()["count"] == 1
    event = memory_audit.json()["events"][0]
    assert event["resource_id"] == superseded.json()["id"]
    assert event["metadata"]["supersedes_id"] == retained.json()["id"]
