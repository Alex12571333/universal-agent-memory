"""Human-readable Obsidian-style vault export."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from memory_plane.contracts.dto import SupersedeMemoryCommand
from memory_plane.domain.models import (
    MemoryItem,
    MemoryRevisionConflictError,
    MemoryStatus,
    Observation,
)
from memory_plane.ports.repositories import MemoryLedger, ObservationRepository
from memory_plane.services.retention import RetentionService


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


@dataclass(frozen=True, slots=True)
class VaultImportSource:
    """One Markdown file supplied by a vault import client."""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class VaultImportChange:
    """One planned or applied import action."""

    path: str
    action: str
    item_id: UUID | None = None
    expected_revision: int | None = None
    new_item_id: UUID | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class VaultImportResult:
    """Safe import result: planned changes plus applied supersede ids."""

    tenant_id: UUID
    workspace_id: UUID
    dry_run: bool
    changes: tuple[VaultImportChange, ...]

    @property
    def supersede_count(self) -> int:
        """Number of files that require or performed a supersede action."""
        return sum(1 for change in self.changes if change.action == "supersede")


class VaultExporter:
    """Export canonical memory into an Obsidian-compatible Markdown vault."""

    def __init__(
        self,
        ledger: MemoryLedger,
        observations: ObservationRepository,
        retention: RetentionService | None = None,
    ) -> None:
        """Bind export to tenant-safe memory and observation repositories."""
        self._ledger = ledger
        self._observations = observations
        self._retention = retention

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

    def plan_import(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        files: tuple[VaultImportSource, ...],
    ) -> VaultImportResult:
        """Inspect Markdown vault files and plan safe CAS supersede operations."""
        changes = tuple(
            self._plan_file_import(tenant_id, workspace_id, file) for file in files
        )
        return VaultImportResult(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            dry_run=True,
            changes=changes,
        )

    def apply_import(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        files: tuple[VaultImportSource, ...],
    ) -> VaultImportResult:
        """Apply a vault import by creating new memory revisions only."""
        if self._retention is None:
            raise RuntimeError("vault import requires a retention service")
        planned = self.plan_import(tenant_id, workspace_id, files)
        applied: list[VaultImportChange] = []
        for change in planned.changes:
            if (
                change.action != "supersede"
                or change.item_id is None
                or change.expected_revision is None
            ):
                applied.append(change)
                continue
            note = _parse_markdown_note(
                next(file.content for file in files if file.path == change.path)
            )
            try:
                result = self._retention.supersede(
                    SupersedeMemoryCommand(
                        tenant_id=tenant_id,
                        item_id=change.item_id,
                        replacement_text=note.body,
                        expected_revision=change.expected_revision,
                        confidence=_optional_float(note.frontmatter.get("confidence")),
                        idempotency_key=f"vault-import:{change.path}:{change.expected_revision}",
                    )
                )
            except MemoryRevisionConflictError as exc:
                applied.append(
                    VaultImportChange(
                        path=change.path,
                        action="conflict",
                        item_id=change.item_id,
                        expected_revision=change.expected_revision,
                        message=str(exc),
                    )
                )
                continue
            applied.append(
                VaultImportChange(
                    path=change.path,
                    action="supersede",
                    item_id=change.item_id,
                    expected_revision=change.expected_revision,
                    new_item_id=result.item.id,
                    message="created new memory revision",
                )
            )
        return VaultImportResult(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            dry_run=False,
            changes=tuple(applied),
        )

    def archive_file(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        file: VaultImportSource,
    ) -> VaultImportResult:
        """Archive a memory note through the same CAS path as human edits."""
        if self._retention is None:
            raise RuntimeError("vault delete requires a retention service")
        change = self._plan_file_archive(tenant_id, workspace_id, file)
        if change.action != "archive" or change.item_id is None or change.expected_revision is None:
            return VaultImportResult(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                dry_run=False,
                changes=(change,),
            )
        note = _parse_markdown_note(file.content)
        try:
            result = self._retention.supersede(
                SupersedeMemoryCommand(
                    tenant_id=tenant_id,
                    item_id=change.item_id,
                    replacement_text=note.body,
                    expected_revision=change.expected_revision,
                    confidence=_optional_float(note.frontmatter.get("confidence")),
                    status=MemoryStatus.ARCHIVED,
                    idempotency_key=f"vault-archive:{file.path}:{change.expected_revision}",
                )
            )
        except MemoryRevisionConflictError as exc:
            change = VaultImportChange(
                path=file.path,
                action="conflict",
                item_id=change.item_id,
                expected_revision=change.expected_revision,
                message=str(exc),
            )
        else:
            change = VaultImportChange(
                path=file.path,
                action="archive",
                item_id=change.item_id,
                expected_revision=change.expected_revision,
                new_item_id=result.item.id,
                message="created archived memory revision",
            )
        return VaultImportResult(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            dry_run=False,
            changes=(change,),
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
        status = "superseded" if superseded_by is not None else item.status.value
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
            "# Obelisk Memory Vault",
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

    def _plan_file_import(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        file: VaultImportSource,
    ) -> VaultImportChange:
        try:
            note = _parse_markdown_note(file.content)
        except ValueError as exc:
            return VaultImportChange(file.path, "error", message=str(exc))

        frontmatter = note.frontmatter
        if frontmatter.get("type") != "memory":
            return VaultImportChange(
                file.path,
                "skip",
                message="only memory notes are imported",
            )
        if frontmatter.get("status") == "superseded":
            return VaultImportChange(
                file.path,
                "skip",
                message="superseded notes are audit history, not import heads",
            )
        try:
            item_id = _parse_note_id(frontmatter.get("id"))
            expected_revision = int(str(frontmatter["revision"]))
            note_tenant_id = UUID(str(frontmatter["tenant_id"]))
            note_workspace_id = UUID(str(frontmatter["workspace_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            return VaultImportChange(
                file.path,
                "error",
                message=f"invalid memory frontmatter: {exc}",
            )
        if note_tenant_id != tenant_id or note_workspace_id != workspace_id:
            return VaultImportChange(
                file.path,
                "error",
                item_id=item_id,
                expected_revision=expected_revision,
                message="tenant/workspace mismatch",
            )
        current = self._ledger.get(tenant_id, item_id)
        if current is None:
            return VaultImportChange(
                file.path,
                "error",
                item_id=item_id,
                expected_revision=expected_revision,
                message="memory item not found",
            )
        if current.revision != expected_revision:
            return VaultImportChange(
                file.path,
                "conflict",
                item_id=item_id,
                expected_revision=expected_revision,
                message=f"expected revision {expected_revision}, actual {current.revision}",
            )
        if current.text.strip() == note.body.strip():
            return VaultImportChange(
                file.path,
                "unchanged",
                item_id=item_id,
                expected_revision=expected_revision,
                message="memory text unchanged",
            )
        return VaultImportChange(
            file.path,
            "supersede",
            item_id=item_id,
            expected_revision=expected_revision,
            message="memory text changed",
        )

    def _plan_file_archive(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        file: VaultImportSource,
    ) -> VaultImportChange:
        try:
            note = _parse_markdown_note(file.content)
        except ValueError as exc:
            return VaultImportChange(file.path, "error", message=str(exc))
        frontmatter = note.frontmatter
        if frontmatter.get("type") != "memory":
            return VaultImportChange(
                file.path,
                "skip",
                message="only memory notes can be archived",
            )
        if frontmatter.get("status") in {"archived", "superseded"}:
            return VaultImportChange(
                file.path,
                "unchanged",
                message="note is already archived or superseded",
            )
        try:
            item_id = _parse_note_id(frontmatter.get("id"))
            expected_revision = int(str(frontmatter["revision"]))
            note_tenant_id = UUID(str(frontmatter["tenant_id"]))
            note_workspace_id = UUID(str(frontmatter["workspace_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            return VaultImportChange(
                file.path,
                "error",
                message=f"invalid memory frontmatter: {exc}",
            )
        if note_tenant_id != tenant_id or note_workspace_id != workspace_id:
            return VaultImportChange(
                file.path,
                "error",
                item_id=item_id,
                expected_revision=expected_revision,
                message="tenant/workspace mismatch",
            )
        current = self._ledger.get(tenant_id, item_id)
        if current is None:
            return VaultImportChange(
                file.path,
                "error",
                item_id=item_id,
                expected_revision=expected_revision,
                message="memory item not found",
            )
        if current.revision != expected_revision:
            return VaultImportChange(
                file.path,
                "conflict",
                item_id=item_id,
                expected_revision=expected_revision,
                message=f"expected revision {expected_revision}, actual {current.revision}",
            )
        return VaultImportChange(
            file.path,
            "archive",
            item_id=item_id,
            expected_revision=expected_revision,
            message="memory will be archived",
        )


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


@dataclass(frozen=True, slots=True)
class _ParsedMarkdownNote:
    frontmatter: dict[str, Any]
    body: str


def _parse_markdown_note(content: str) -> _ParsedMarkdownNote:
    """Parse the subset of Markdown/frontmatter emitted by the vault exporter."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing YAML frontmatter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line == "---")
    except StopIteration as exc:
        raise ValueError("unterminated YAML frontmatter") from exc
    frontmatter = _parse_frontmatter(lines[1:end])
    body_lines = lines[end + 1 :]
    body: list[str] = []
    for line in body_lines:
        if line in {"## Provenance", "## Quote", "## Links", "## Evidence"}:
            break
        body.append(line)
    return _ParsedMarkdownNote(
        frontmatter=frontmatter,
        body="\n".join(body).strip(),
    )


def _parse_frontmatter(lines: list[str]) -> dict[str, Any]:
    """Parse conservative YAML scalars and list blocks without a YAML dependency."""
    result: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith(" ") or ":" not in line:
            raise ValueError(f"unsupported frontmatter line: {line}")
        key, raw_value = line.split(":", 1)
        raw_value = raw_value.strip()
        if raw_value == "":
            values: list[Any] = []
            index += 1
            while index < len(lines) and lines[index].startswith("  - "):
                values.append(_parse_yaml_scalar(lines[index][4:].strip()))
                index += 1
            result[key] = values
            continue
        result[key] = _parse_yaml_scalar(raw_value)
        index += 1
    return result


def _parse_yaml_scalar(value: str) -> Any:
    """Parse one scalar from exporter-generated YAML."""
    if value == "null":
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _parse_note_id(value: Any) -> UUID:
    """Parse `mem-<uuid>` note ids from frontmatter."""
    text = str(value)
    if not text.startswith("mem-"):
        raise ValueError("memory id must start with mem-")
    return UUID(text.removeprefix("mem-"))


def _optional_float(value: Any) -> float | None:
    """Return a bounded float when frontmatter contains a numeric confidence."""
    if value is None:
        return None
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise ValueError("confidence must be between 0 and 1")
    return parsed
