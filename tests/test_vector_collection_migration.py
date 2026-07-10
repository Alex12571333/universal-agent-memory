from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import migrate_vector_collection  # noqa: E402


def test_vector_collection_migration_builds_verifies_and_reports(monkeypatch) -> None:
    embedding = SimpleNamespace(
        reindex_all=Mock(return_value=7),
        indexed_workspace_count=Mock(return_value=7),
        model_name="provider/model-v2",
    )
    build = Mock(return_value=SimpleNamespace(embedding=embedding))
    monkeypatch.setattr(migrate_vector_collection, "build_postgres_container", build)
    monkeypatch.setenv("UAM_QDRANT_COLLECTION", "memory_items_v1")
    tenant_id = uuid4()
    workspace_id = uuid4()

    report = migrate_vector_collection.migrate_collection(
        dsn="postgresql://memory",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        qdrant_url="http://qdrant:6333",
        target_collection="memory_items_v2",
        dimension=3072,
    )

    build.assert_called_once_with(
        "postgresql://memory",
        server_id=tenant_id,
        project_id=workspace_id,
        qdrant_url="http://qdrant:6333",
        qdrant_dim=3072,
        qdrant_collection="memory_items_v2",
        require_qdrant=True,
    )
    assert report["ok"] is True
    assert report["source_collection"] == "memory_items_v1"
    assert report["target_collection"] == "memory_items_v2"
    assert report["embedding_model"] == "provider/model-v2"
    assert report["indexed_points"] == report["verified_points"] == 7


def test_vector_collection_migration_rejects_active_target(monkeypatch) -> None:
    monkeypatch.setenv("UAM_QDRANT_COLLECTION", "memory_items")

    with pytest.raises(ValueError, match="must differ"):
        migrate_vector_collection.migrate_collection(
            dsn="postgresql://memory",
            tenant_id=uuid4(),
            workspace_id=uuid4(),
            qdrant_url="http://qdrant:6333",
            target_collection="memory_items",
            dimension=1536,
        )


def test_vector_collection_migration_rejects_failed_count_verification(monkeypatch) -> None:
    embedding = SimpleNamespace(
        reindex_all=Mock(return_value=7),
        indexed_workspace_count=Mock(return_value=6),
        model_name="provider/model-v2",
    )
    monkeypatch.setattr(
        migrate_vector_collection,
        "build_postgres_container",
        Mock(return_value=SimpleNamespace(embedding=embedding)),
    )
    monkeypatch.setenv("UAM_QDRANT_COLLECTION", "memory_items_v1")

    with pytest.raises(RuntimeError, match="verification failed"):
        migrate_vector_collection.migrate_collection(
            dsn="postgresql://memory",
            tenant_id=uuid4(),
            workspace_id=uuid4(),
            qdrant_url="http://qdrant:6333",
            target_collection="memory_items_v2",
            dimension=3072,
        )
