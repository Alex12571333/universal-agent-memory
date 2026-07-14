from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from memory_plane.api.app import DEFAULT_PROJECT_ID, DEFAULT_SERVER_ID, create_app
from memory_plane.bootstrap import build_postgres_container

DATABASE_URL = os.getenv("UAM_TEST_DATABASE_URL")


@pytest.mark.skipif(not DATABASE_URL, reason="set UAM_TEST_DATABASE_URL to run PostgreSQL tests")
def test_authorization_denial_is_durable_in_postgres(monkeypatch) -> None:
    """Prove middleware denial records survive outside the API process."""
    monkeypatch.delenv("UAM_API_KEY", raising=False)
    monkeypatch.delenv("UAM_API_KEYS", raising=False)
    monkeypatch.delenv("UAM_API_KEY_FILE", raising=False)
    monkeypatch.delenv("UAM_API_KEYS_FILE", raising=False)
    monkeypatch.delenv("UAM_QDRANT_URL", raising=False)
    container = build_postgres_container(
        DATABASE_URL or "",
        server_id=DEFAULT_SERVER_ID,
        project_id=DEFAULT_PROJECT_ID,
    )
    try:
        with container.store._connection() as connection:
            role = connection.execute(
                """
                select current_user as username, r.rolsuper
                from pg_roles r
                where r.rolname = current_user
                """
            ).fetchone()
        assert role is not None
        assert role["rolsuper"] is False
        client = TestClient(create_app(container, api_key="integration-auth-secret"))
        response = client.get(
            "/v1/settings/models?credential=must-not-be-audited",
            headers={"Authorization": "Bearer invalid-auth-secret"},
        )

        assert response.status_code == 401
        events = container.audit.list_events(
            DEFAULT_SERVER_ID,
            action="auth.request.denied",
            limit=10,
        )
        event = next(
            row
            for row in events
            if row.resource_id == "/v1/settings"
            and row.metadata.get("reason") == "invalid_credential"
        )
        assert event.status == "denied"
        assert event.actor == "anonymous"
        assert event.actor_type == "unauthenticated"
        assert "must-not-be-audited" not in str(event)
        assert "invalid-auth-secret" not in str(event)
        assert "integration-auth-secret" not in str(event)
    finally:
        container.store.close()
