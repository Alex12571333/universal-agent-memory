from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from memory_plane.bootstrap import build_in_memory_container
from memory_plane.contracts.dto import RetainCommand, SupersedeMemoryCommand
from memory_plane.domain.models import (
    MemoryLayer,
    MemoryRevisionConflictError,
    MemoryScope,
    Provenance,
)
from memory_plane.services.vault import (
    VaultImportSource,
    VaultPatchCommand,
    VaultWriteResult,
    editable_vault_content,
)


def test_vault_export_renders_memory_frontmatter_and_reflection_links() -> None:
    container = build_in_memory_container()
    tenant = uuid4()
    workspace = uuid4()
    old = container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text='Release Alpha is July 15 "draft".',
            provenance=Provenance(
                source_kind="document",
                origin_uri="file:///roadmap.md",
                quote="Release Alpha is July 15.",
            ),
            labels=("release", "alpha"),
        )
    )
    new = container.retention.supersede(
        SupersedeMemoryCommand(
            tenant_id=tenant,
            item_id=old.item.id,
            replacement_text="Release Alpha is July 16.",
            expected_revision=1,
            confidence=0.91,
        )
    )
    container.reflection.reflect(tenant, workspace)

    vault = container.vault.export(tenant, workspace)
    files = {row.path: row.content for row in vault.files}
    memory_paths = [path for path in files if path.startswith("semantic/")]
    reflection_paths = [path for path in files if path.startswith("reflections/")]

    assert "README.md" in files
    assert "memories: 2" in files["README.md"]
    assert "observations: 2" in files["README.md"]
    assert len(memory_paths) == 2
    assert len(reflection_paths) == 2

    replacement_note = next(
        content
        for path, content in files.items()
        if path.startswith("semantic/") and "Release Alpha is July 16." in content
    )
    assert 'type: "memory"' in replacement_note
    assert "revision: 2" in replacement_note
    assert f'supersedes_id: "{old.item.id}"' in replacement_note
    assert f"- supersedes: [[mem-{old.item.id}]]" in replacement_note
    assert "confidence: 0.91" in replacement_note

    stale_observation = next(
        content
        for content in files.values()
        if "Release Alpha is July 15" in content and 'type: "observation"' in content
    )
    assert "stale: true" in stale_observation
    assert f"- [[mem-{old.item.id}]]" in stale_observation
    assert f"- [[mem-{new.item.id}]]" not in stale_observation


def test_vault_export_can_write_obsidian_folder(tmp_path: Path) -> None:
    container = build_in_memory_container()
    tenant = uuid4()
    workspace = uuid4()
    container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=MemoryLayer.CORE,
            scope=MemoryScope.WORKSPACE,
            kind="decision",
            text="Use PostgreSQL as the canonical ledger.",
            provenance=Provenance(source_kind="api"),
        )
    )

    result = container.vault.export_workspace(tenant, workspace, tmp_path)

    assert isinstance(result, VaultWriteResult)
    assert result.memory_count == 1
    assert result.observation_count == 0
    assert result.files_written == 2
    assert (tmp_path / "README.md").exists()
    exported = list((tmp_path / "core").glob("mem-*.md"))
    assert len(exported) == 1
    assert "Use PostgreSQL" in exported[0].read_text(encoding="utf-8")


def test_vault_import_dry_run_detects_changed_memory_note() -> None:
    container = build_in_memory_container()
    tenant = uuid4()
    workspace = uuid4()
    container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text="Alpha release is July 15.",
            provenance=Provenance(source_kind="api"),
        )
    )
    export = container.vault.export(tenant, workspace)
    note = next(file for file in export.files if file.path.startswith("semantic/"))
    edited = note.content.replace("Alpha release is July 15.", "Alpha release is July 16.")

    plan = container.vault.plan_import(
        tenant,
        workspace,
        (VaultImportSource(note.path, edited),),
    )

    assert plan.dry_run is True
    assert plan.supersede_count == 1
    assert len(plan.changes) == 1
    assert plan.changes[0].action == "supersede"
    assert plan.changes[0].new_item_id is None


def test_vault_import_ignores_embedding_sections_from_editable_body() -> None:
    container = build_in_memory_container()
    tenant = uuid4()
    workspace = uuid4()
    container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text="Редактор показывает только человеческий текст.",
            provenance=Provenance(source_kind="api"),
        )
    )
    export = container.vault.export(tenant, workspace)
    note = next(file for file in export.files if file.path.startswith("semantic/"))
    edited = note.content.replace(
        "Редактор показывает только человеческий текст.",
        "\n\n".join(
            [
                "Редактор сохраняет только чистый текст.",
                "## Embedding\n[0.1, 0.2, 0.3]",
                "## Metadata\ntechnical payload",
            ]
        ),
    )

    result = container.vault.apply_import(
        tenant,
        workspace,
        (VaultImportSource(note.path, edited),),
    )

    memories = container.store.list_for_workspace(tenant, workspace)
    assert result.changes[0].action == "supersede"
    assert any(row.text == "Редактор сохраняет только чистый текст." for row in memories)
    assert all("[0.1, 0.2, 0.3]" not in row.text for row in memories)
    assert all("technical payload" not in row.text for row in memories)


def test_vault_import_strips_leaked_vector_payload_without_heading() -> None:
    container = build_in_memory_container()
    tenant = uuid4()
    workspace = uuid4()
    container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text="Редактор должен показывать обычный текст.",
            provenance=Provenance(source_kind="api"),
        )
    )
    export = container.vault.export(tenant, workspace)
    note = next(file for file in export.files if file.path.startswith("semantic/"))
    edited = note.content.replace(
        "Редактор должен показывать обычный текст.",
        "\n".join(
            [
                "Редактор хранит только понятный человеку текст.",
                "embedding: [0.11, 0.22, 0.33, 0.44]",
                "metadata: {\"provider\":\"qwen-on-spark\"}",
                "[0.91, 0.82, 0.73, 0.64, 0.55, 0.46]",
                "checksum_sha256: deadbeef",
            ]
        ),
    )

    result = container.vault.apply_import(
        tenant,
        workspace,
        (VaultImportSource(note.path, edited),),
    )

    memories = container.store.list_for_workspace(tenant, workspace)
    assert result.changes[0].action == "supersede"
    assert any(
        row.text == "Редактор хранит только понятный человеку текст."
        for row in memories
    )
    assert all("0.91, 0.82" not in row.text for row in memories)
    assert all("qwen-on-spark" not in row.text for row in memories)


def test_vault_import_strips_multiline_vector_array_from_editable_body() -> None:
    container = build_in_memory_container()
    tenant = uuid4()
    workspace = uuid4()
    container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text="Редактор хранит человеческий текст.",
            provenance=Provenance(source_kind="api"),
        )
    )
    export = container.vault.export(tenant, workspace)
    note = next(file for file in export.files if file.path.startswith("semantic/"))
    edited = note.content.replace(
        "Редактор хранит человеческий текст.",
        "\n".join(
            [
                "Редактор показывает только смысловую заметку.",
                "[",
                "0.11,",
                "0.22,",
                "0.33,",
                "]",
                "Обычная строка после вектора остается.",
            ]
        ),
    )

    result = container.vault.apply_import(
        tenant,
        workspace,
        (VaultImportSource(note.path, edited),),
    )

    memories = container.store.list_for_workspace(tenant, workspace)
    assert result.changes[0].action == "supersede"
    assert any(
        row.text
        == "Редактор показывает только смысловую заметку.\nОбычная строка после вектора остается."
        for row in memories
    )
    assert all("0.11" not in row.text for row in memories)


def test_editable_vault_content_hides_qdrant_and_fenced_vector_payloads() -> None:
    note = """---
id: "mem-demo"
type: "memory"
status: "active"
revision: 4
---

Пользователь хочет редактировать только обычный текст.

payload: {
  "qdrant": true,
  "vector": [0.1, 0.2, 0.3, 0.4],
  "model_name": "jina-embeddings-v4"
}

```json
{"embedding": [0.5, 0.6, 0.7, 0.8], "dimension": 2048}
```

dimension: 2048
score: 0.91
Понятная строка после служебных данных.

## Provenance
- source: api
"""

    editable = editable_vault_content(note)

    assert "Пользователь хочет редактировать только обычный текст." in editable
    assert "Понятная строка после служебных данных." in editable
    assert "qdrant" not in editable
    assert "embedding" not in editable
    assert "2048" not in editable


def test_vault_import_apply_creates_superseding_revision_without_overwrite() -> None:
    container = build_in_memory_container()
    tenant = uuid4()
    workspace = uuid4()
    retained = container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=MemoryLayer.CORE,
            scope=MemoryScope.WORKSPACE,
            kind="decision",
            text="Use fake embeddings in production.",
            provenance=Provenance(source_kind="api"),
        )
    )
    export = container.vault.export(tenant, workspace)
    note = next(file for file in export.files if file.path.startswith("core/"))
    edited = note.content.replace(
        "Use fake embeddings in production.",
        "Use versioned production embeddings.",
    )

    result = container.vault.apply_import(
        tenant,
        workspace,
        (VaultImportSource(note.path, edited),),
    )
    memories = container.store.list_for_workspace(tenant, workspace)
    new_item = next(
        item for item in memories if item.text == "Use versioned production embeddings."
    )

    assert result.dry_run is False
    assert result.changes[0].action == "supersede"
    assert result.changes[0].new_item_id == new_item.id
    assert new_item.revision == 2
    assert new_item.supersedes_id == retained.item.id
    assert any(item.text == "Use fake embeddings in production." for item in memories)


def test_vault_import_reports_conflict_for_stale_export() -> None:
    container = build_in_memory_container()
    tenant = uuid4()
    workspace = uuid4()
    retained = container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text="Beta launches Monday.",
            provenance=Provenance(source_kind="api"),
        )
    )
    export = container.vault.export(tenant, workspace)
    stale_note = next(file for file in export.files if file.path.startswith("semantic/"))
    container.retention.supersede(
        SupersedeMemoryCommand(
            tenant_id=tenant,
            item_id=retained.item.id,
            replacement_text="Beta launches Tuesday.",
            expected_revision=1,
        )
    )
    edited = stale_note.content.replace("Beta launches Monday.", "Beta launches Wednesday.")

    result = container.vault.apply_import(
        tenant,
        workspace,
        (VaultImportSource(stale_note.path, edited),),
    )

    assert result.changes[0].action == "conflict"
    assert result.changes[0].expected_revision == 1
    assert "actual 2" in result.changes[0].message


def test_vault_targeted_patch_replaces_one_section_and_is_idempotent() -> None:
    container = build_in_memory_container()
    tenant = uuid4()
    workspace = uuid4()
    retained = container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="project_note",
            text="# Проект\n\n## Решение\nСтарая модель.\n\n## Ограничения\nТолько локально.",
            provenance=Provenance(source_kind="operator-ui"),
        )
    )
    command = VaultPatchCommand(
        tenant_id=tenant,
        workspace_id=workspace,
        item_id=retained.item.id,
        expected_revision=1,
        section_heading="Решение",
        section_content="Новая локальная модель.",
    )

    first = container.vault.patch_memory(command)
    retry = container.vault.patch_memory(command)

    assert first.changed is True
    assert first.item.revision == 2
    assert first.item.supersedes_id == retained.item.id
    assert "## Решение\nНовая локальная модель." in first.item.text
    assert "## Ограничения\nТолько локально." in first.item.text
    assert first.queued_event_ids
    assert retry.changed is False
    assert retry.item.id == first.item.id
    assert retry.queued_event_ids == ()


def test_vault_targeted_patch_rejects_system_section_and_stale_revision() -> None:
    container = build_in_memory_container()
    tenant = uuid4()
    workspace = uuid4()
    retained = container.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=MemoryLayer.CORE,
            scope=MemoryScope.WORKSPACE,
            kind="decision",
            text="## Решение\nИспользовать локальный сервер.",
            provenance=Provenance(source_kind="api"),
        )
    )

    with pytest.raises(ValueError, match="system-managed"):
        container.vault.patch_memory(
            VaultPatchCommand(
                tenant_id=tenant,
                workspace_id=workspace,
                item_id=retained.item.id,
                expected_revision=1,
                section_heading="Provenance",
                section_content="Поддельный источник",
            )
        )

    container.vault.patch_memory(
        VaultPatchCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            item_id=retained.item.id,
            expected_revision=1,
            replace_body="Обновлённое решение.",
        )
    )
    with pytest.raises(MemoryRevisionConflictError):
        container.vault.patch_memory(
            VaultPatchCommand(
                tenant_id=tenant,
                workspace_id=workspace,
                item_id=retained.item.id,
                expected_revision=1,
                replace_body="Конкурирующее решение.",
            )
        )
