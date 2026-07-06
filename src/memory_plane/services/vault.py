"""Human-readable Obsidian-style vault export."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from memory_plane.domain.models import MemoryItem, Observation
from memory_plane.ports.repositories import MemoryLedger, ObservationRepository


@dataclass(frozen=True, slots=True)
class VaultFile:
    """One generated Markdown file inside a vault export."""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class VaultExport:
    """A deterministic, filesystem-ready projection of one workspace."""

    tenant_id: UUID
    workspace_id: UUID
    files: tuple[VaultFile, ...]


@dataclass(frozen=True, slots=True)
class VaultWriteResult:
    """Summary after materializing an export to disk."""

    root: Path
    files_written: int
    memory_count: int
    observation_count: int


class VaultExporter:
    """Export canonical memory into an Obsidian-compatible Markdown vault."""

    def __init__(
        self,
        ledger: MemoryLedger,
        observations: ObservationRepository,
    ) -> None:
        """Bind export to tenant-safe memory and observation repositories."""
        self._ledger = ledger
        self._observations = observations

    def export(self, tenant_id: UUID, workspace_id: UUID) -> VaultExport:
        """Render all memories and observations for a workspace."""
        items = self._ledger.list_for_workspace(tenant_id, workspace_id)
        observations = self._observations.list_for_workspace(tenant_id, workspace_id)
        superseded_by = self._superseded_by(items)

        files: list[VaultFile] = [
            VaultFile(
                "README.md",
                self._render_index(tenant_id, workspace_id, items, observations),
            )
        ]
        files.extend(
            self._memory_file(item, superseded_by=superseded_by.get(item.id))
            for item in items
        )
        files.extend(self._observation_file(observation) for observation in observations)
        return VaultExport(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            files=tuple(sorted(files, key=lambda row: row.path)),
        )

    def export_workspace(
        self, tenant_id: UUID, workspace_id: UUID, root: Path
    ) -> VaultWriteResult:
        """Materialize a workspace export as Markdown files."""
        export = self.export(tenant_id, workspace_id)
        for file in export.files:
            relative_path = Path(file.path)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise ValueError(f"unsafe vault path: {file.path}")
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file.content, encoding="utf-8")
        return VaultWriteResult(
            root=root,
            files_written=len(export.files),
            memory_count=sum(
                1 for file in export.files if file.path.startswith(self._MEMORY_PREFIXES)
            ),
            observation_count=sum(
                1 for file in export.files if file.path.startswith("reflections/")
            ),
        )

    _MEMORY_PREFIXES = (
        "working/",
        "core/",
        "episodic/",
        "semantic/",
        "procedural/",
        "social/",
        "reflection/",
        "error/",
    )

    @classmethod
    def _memory_file(
        cls, item: MemoryItem, *, superseded_by: UUID | None
    ) -> VaultFile:
        """Render a canonical MemoryItem as one Markdown note."""
        path = f"{item.layer.value}/{cls._memory_name(item.id)}.md"
        status = "superseded" if superseded_by is not None else "active"
        links: list[str] = []
        if item.supersedes_id is not None:
            links.append(f"- supersedes: [[{cls._memory_name(item.supersedes_id)}]]")
        if superseded_by is not None:
            links.append(f"- superseded_by: [[{cls._memory_name(superseded_by)}]]")

        body = [
            _frontmatter(
                {
                    "id": f"mem-{item.id}",
                    "type": "memory",
                    "status": status,
                    "layer": item.layer.value,
                    "scope": item.scope.value,
                    "kind": item.kind,
                    "tenant_id": str(item.tenant_id),
                    "workspace_id": str(item.workspace_id),
                    "agent_id": _optional_uuid(item.agent_id),
                    "thread_id": _optional_uuid(item.thread_id),
                    "labels": list(item.labels),
                    "importance": item.importance,
                    "salience": item.salience,
                    "confidence": item.confidence,
                    "revision": item.revision,
                    "supersedes_id": _optional_uuid(item.supersedes_id),
                    "superseded_by": _optional_uuid(superseded_by),
                    "observed_at": _optional_datetime(item.observed_at),
                    "valid_from": _optional_datetime(item.valid_from),
                    "valid_to": _optional_datetime(item.valid_to),
                    "created_at": _optional_datetime(item.created_at),
                    "source_kind": item.provenance.source_kind,
                    "origin_uri": item.provenance.origin_uri,
                    "object_key": item.provenance.object_key,
                    "checksum_sha256": item.provenance.checksum_sha256,
                    "extraction_version": item.provenance.extraction_version,
                    "metadata": item.metadata,
                }
            ),
            "",
            item.text.strip(),
            "",
            "## Provenance",
            f"- source: {item.provenance.source_kind}",
        ]
        if item.provenance.origin_uri:
            body.append(f"- origin: {item.provenance.origin_uri}")
        if item.provenance.object_key:
            body.append(f"- object: {item.provenance.object_key}")
        if item.provenance.checksum_sha256:
            body.append(f"- checksum: `{item.provenance.checksum_sha256}`")
        if item.provenance.quote:
            body.extend(["", "## Quote", _blockquote(item.provenance.quote)])
        if links:
            body.extend(["", "## Links", *links])
        return VaultFile(path=path, content="\n".join(body).rstrip() + "\n")

    @classmethod
    def _observation_file(cls, observation: Observation) -> VaultFile:
        """Render one reflection observation as an auditable Markdown note."""
        path = f"reflections/{cls._observation_name(observation.id)}.md"
        evidence = [
            f"- [[{cls._memory_name(item_id)}]]"
            for item_id in observation.evidence_ids
        ]
        body = [
            _frontmatter(
                {
                    "id": f"obs-{observation.id}",
                    "type": "observation",
                    "status": "stale" if observation.stale else "active",
                    "tenant_id": str(observation.tenant_id),
                    "workspace_id": str(observation.workspace_id),
                    "confidence": observation.confidence,
                    "stale": observation.stale,
                    "evidence_ids": [str(item_id) for item_id in observation.evidence_ids],
                    "created_at": _optional_datetime(observation.created_at),
                }
            ),
            "",
            observation.summary.strip(),
            "",
            "## Evidence",
            *evidence,
        ]
        return VaultFile(path=path, content="\n".join(body).rstrip() + "\n")

    @classmethod
    def _render_index(
        cls,
        tenant_id: UUID,
        workspace_id: UUID,
        items: tuple[MemoryItem, ...],
        observations: tuple[Observation, ...],
    ) -> str:
        """Render a small vault landing page for humans and agents."""
        by_layer: dict[str, int] = {}
        for item in items:
            by_layer[item.layer.value] = by_layer.get(item.layer.value, 0) + 1
        memory_links = [
            f"- [[{cls._memory_name(item.id)}]] — {item.layer.value}: {item.text[:96]}"
            for item in items
        ]
        observation_links = [
            f"- [[{cls._observation_name(row.id)}]] — {'stale' if row.stale else 'active'}"
            for row in observations
        ]
        layer_lines = [f"- {layer}: {count}" for layer, count in sorted(by_layer.items())]
        stale_count = sum(1 for row in observations if row.stale)
        body = [
            _frontmatter(
                {
                    "type": "index",
                    "tenant_id": str(tenant_id),
                    "workspace_id": str(workspace_id),
                }
            ),
            "",
            "# Universal Agent Memory Vault",
            "",
            f"- tenant: `{tenant_id}`",
            f"- workspace: `{workspace_id}`",
            f"- memories: {len(items)}",
            f"- observations: {len(observations)}",
            f"- stale observations: {stale_count}",
            "",
            "## Layers",
            *(layer_lines or ["- none: 0"]),
            "",
            "## Memories",
            *(memory_links or ["- none"]),
            "",
            "## Observations",
            *(observation_links or ["- none"]),
            "",
            "## Conventions",
            "- `mem-*` notes are append-only canonical memories.",
            "- `obs-*` notes are reflection outputs with evidence links.",
            "- Edit by superseding through the API; do not rewrite history in place.",
        ]
        return "\n".join(body).rstrip() + "\n"

    @staticmethod
    def _superseded_by(items: tuple[MemoryItem, ...]) -> dict[UUID, UUID]:
        result: dict[UUID, UUID] = {}
        for item in items:
            if item.supersedes_id is not None:
                result[item.supersedes_id] = item.id
        return result

    @staticmethod
    def _memory_name(item_id: UUID) -> str:
        return f"mem-{item_id}"

    @staticmethod
    def _observation_name(observation_id: UUID) -> str:
        return f"obs-{observation_id}"


def _frontmatter(values: dict[str, Any]) -> str:
    """Render dependency-free YAML-compatible frontmatter for simple values."""
    lines = ["---"]
    for key, value in values.items():
        rendered = _yaml_value(value, indent=0)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines)


def _yaml_value(value: Any, *, indent: int) -> str:
    """Render YAML scalars, lists and shallow dicts without dependencies."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list | tuple):
        if not value:
            return "[]"
        pad = " " * (indent + 2)
        return "\n" + "\n".join(
            f"{pad}- {_yaml_value(item, indent=indent + 2)}" for item in value
        )
    if isinstance(value, dict):
        if not value:
            return "{}"
        pad = " " * (indent + 2)
        return "\n" + "\n".join(
            f"{pad}{key}: {_yaml_value(val, indent=indent + 2)}"
            for key, val in sorted(value.items())
        )
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _optional_uuid(value: UUID | None) -> str | None:
    """Render optional UUID values without leaking Python reprs."""
    return str(value) if value is not None else None


def _optional_datetime(value: datetime | None) -> str | None:
    """Render optional datetime values as ISO-8601 strings."""
    return value.isoformat() if value is not None else None


def _blockquote(text: str) -> str:
    """Render provenance quotes without breaking Markdown structure."""
    return "\n".join(f"> {line}" for line in text.splitlines())
