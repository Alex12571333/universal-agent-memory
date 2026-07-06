from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from memory_plane.bootstrap import build_in_memory_container
from memory_plane.contracts.dto import RetainCommand, SupersedeMemoryCommand
from memory_plane.domain.models import MemoryLayer, MemoryScope, Provenance
from memory_plane.services.vault import VaultWriteResult


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
