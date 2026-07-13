from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from memory_plane.domain.models import MemoryStatus


def _load_probe():
    path = Path(__file__).resolve().parents[1] / "scripts" / "restore_reindex_probe.py"
    spec = importlib.util.spec_from_file_location("restore_reindex_probe_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_restored_reindex_probe_requires_dense_recall(monkeypatch) -> None:
    probe = _load_probe()
    tenant_id, workspace_id, memory_id = uuid4(), uuid4(), uuid4()
    item = SimpleNamespace(
        id=memory_id,
        text="restored semantic memory for recovery",
        status=MemoryStatus.ACTIVE,
        supersedes_id=None,
    )
    candidate = SimpleNamespace(item=item, semantic=0.91)
    container = SimpleNamespace(
        store=SimpleNamespace(list_for_workspace=lambda *_args: (item,)),
        vault_health=SimpleNamespace(
            inspect=lambda *_args: SimpleNamespace(
                error_count=0, warning_count=0, healthy=True, issues=()
            )
        ),
        embedding=SimpleNamespace(
            reindex_all=lambda *_args: 1,
            indexed_workspace_count=lambda *_args: 1,
            model_name="test-embed",
        ),
        retrieval=SimpleNamespace(
            recall=lambda _query: SimpleNamespace(
                candidates=(candidate,), sources_used=("postgres_lexical", "qdrant_hybrid")
            )
        ),
    )
    monkeypatch.setattr(probe, "build_postgres_container", lambda *_args, **_kwargs: container)

    report = probe.run_probe(
        dsn="postgresql://restored/memory",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        qdrant_url="http://qdrant:6333",
        collection="recovery_probe",
        dimension=8,
    )

    assert report["ok"] is True
    assert report["indexed_points"] == 1
    assert [check["name"] for check in report["checks"]] == [
        "canonical-vault-health",
        "restored-reindex",
        "semantic-recall",
    ]


def test_restored_reindex_probe_rejects_lexical_only_result(monkeypatch) -> None:
    probe = _load_probe()
    tenant_id, workspace_id, memory_id = uuid4(), uuid4(), uuid4()
    item = SimpleNamespace(
        id=memory_id,
        text="restored semantic memory",
        status=MemoryStatus.ACTIVE,
        supersedes_id=None,
    )
    container = SimpleNamespace(
        store=SimpleNamespace(list_for_workspace=lambda *_args: (item,)),
        vault_health=SimpleNamespace(
            inspect=lambda *_args: SimpleNamespace(
                error_count=0, warning_count=0, healthy=True, issues=()
            )
        ),
        embedding=SimpleNamespace(
            reindex_all=lambda *_args: 1,
            indexed_workspace_count=lambda *_args: 1,
            model_name="test-embed",
        ),
        retrieval=SimpleNamespace(
            recall=lambda _query: SimpleNamespace(
                candidates=(SimpleNamespace(item=item, semantic=0.0),),
                sources_used=("postgres_lexical",),
            )
        ),
    )
    monkeypatch.setattr(probe, "build_postgres_container", lambda *_args, **_kwargs: container)

    report = probe.run_probe(
        dsn="postgresql://restored/memory",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        qdrant_url="http://qdrant:6333",
        collection="recovery_probe",
        dimension=8,
    )

    assert report["ok"] is False
    assert report["checks"][2] == {"name": "semantic-recall", "ok": False}


def test_restored_reindex_probe_rejects_broken_canonical_vault(monkeypatch) -> None:
    probe = _load_probe()
    tenant_id, workspace_id = uuid4(), uuid4()
    item = SimpleNamespace(
        id=uuid4(),
        text="restored semantic memory",
        status=MemoryStatus.ACTIVE,
        supersedes_id=None,
    )
    container = SimpleNamespace(
        store=SimpleNamespace(list_for_workspace=lambda *_args: (item,)),
        vault_health=SimpleNamespace(
            inspect=lambda *_args: SimpleNamespace(
                error_count=1,
                warning_count=0,
                healthy=False,
                issues=(SimpleNamespace(code="missing_provenance", severity="error"),),
            )
        ),
        embedding=SimpleNamespace(reindex_all=lambda *_args: 1),
        retrieval=SimpleNamespace(recall=lambda _query: None),
    )
    monkeypatch.setattr(probe, "build_postgres_container", lambda *_args, **_kwargs: container)

    try:
        probe.run_probe(
            dsn="postgresql://restored/memory",
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            qdrant_url="http://qdrant:6333",
            collection="recovery_probe",
            dimension=8,
        )
    except RuntimeError as exc:
        assert "missing_provenance" in str(exc)
    else:
        raise AssertionError("expected broken restored vault rejection")


def test_restored_reindex_probe_rejects_active_collection(monkeypatch) -> None:
    probe = _load_probe()
    monkeypatch.setenv("UAM_QDRANT_COLLECTION", "memory_items")

    try:
        probe.run_probe(
            dsn="postgresql://restored/memory",
            tenant_id=uuid4(),
            workspace_id=uuid4(),
            qdrant_url="http://qdrant:6333",
            collection="memory_items",
            dimension=8,
        )
    except ValueError as exc:
        assert "must differ" in str(exc)
    else:
        raise AssertionError("expected active collection rejection")
