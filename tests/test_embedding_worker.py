from __future__ import annotations

from uuid import uuid4

from pytest import MonkeyPatch

from memory_plane.workers import embedding_main


def test_embedding_worker_uses_configured_qdrant_identity(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("UAM_QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("UAM_QDRANT_COLLECTION", "jina_v4_2048")
    monkeypatch.setenv("UAM_EMBEDDING_DIM", "2048")
    monkeypatch.setattr(
        embedding_main,
        "build_postgres_container",
        lambda *args, **kwargs: captured.update(kwargs),
    )

    embedding_main._build_container("postgresql://example", server_id=uuid4(), project_id=uuid4())

    assert captured["qdrant_url"] == "http://qdrant:6333"
    assert captured["qdrant_collection"] == "jina_v4_2048"
    assert captured["qdrant_dim"] == 2048
    assert captured["require_qdrant"] is True
