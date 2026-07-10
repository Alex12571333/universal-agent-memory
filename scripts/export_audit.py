"""Export recent audit events as a tamper-evident forensic bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from memory_plane.adapters.postgres import PostgresMemoryLedger
from memory_plane.api.app import DEFAULT_PROJECT_ID, DEFAULT_SERVER_ID
from memory_plane.domain.audit import AuditEvent
from memory_plane.services.audit import AuditLogService

BUNDLE_FORMAT = "obelisk-audit-export-v1"


def main() -> int:
    """Export operator audit events into JSONL plus checksum manifest files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", help="Directory where audit bundle files are written")
    parser.add_argument(
        "--database-url",
        default=os.getenv("UAM_DATABASE_URL"),
        help="PostgreSQL app-role URL; defaults to UAM_DATABASE_URL",
    )
    parser.add_argument(
        "--tenant-id",
        type=UUID,
        default=UUID(os.getenv("UAM_SERVER_ID", str(DEFAULT_SERVER_ID))),
        help="Tenant/server UUID to export",
    )
    parser.add_argument(
        "--workspace-id",
        type=UUID,
        default=UUID(os.getenv("UAM_PROJECT_ID", str(DEFAULT_PROJECT_ID))),
        help="Workspace/project UUID filter",
    )
    parser.add_argument("--all-workspaces", action="store_true", help="Do not filter by workspace")
    parser.add_argument("--action", help="Audit action filter, for example memory.retain")
    parser.add_argument("--resource-type", help="Resource type filter, for example memory_item")
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum events to export; repository cap is 500",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("database URL is required")

    ledger = PostgresMemoryLedger(args.database_url)
    ledger.connect()
    audit = AuditLogService(ledger)
    workspace_id = None if args.all_workspaces else args.workspace_id
    events = audit.list_events(
        args.tenant_id,
        workspace_id=workspace_id,
        action=args.action,
        resource_type=args.resource_type,
        limit=args.limit,
    )

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    events_path = output / "audit-events.jsonl"
    manifest_path = output / "manifest.json"
    manifest_checksum_path = output / "manifest.sha256"

    events_bytes = _jsonl_bytes(events)
    events_path.write_bytes(events_bytes)
    manifest = _manifest(
        events=events,
        events_bytes=events_bytes,
        tenant_id=args.tenant_id,
        workspace_id=workspace_id,
        action=args.action,
        resource_type=args.resource_type,
        requested_limit=args.limit,
    )
    manifest_bytes = _canonical_json_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    manifest_checksum_path.write_text(
        f"{_sha256(manifest_bytes)}  manifest.json\n",
        encoding="utf-8",
    )

    print(
        "audit_export=PASS "
        f"events={len(events)} output={output} "
        f"manifest_sha256={_sha256(manifest_bytes)}"
    )
    return 0


def _jsonl_bytes(events: tuple[AuditEvent, ...]) -> bytes:
    """Serialize audit events as stable newline-delimited JSON."""
    rows = [_json_ready(event) for event in events]
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )
    return payload.encode("utf-8")


def _manifest(
    *,
    events: tuple[AuditEvent, ...],
    events_bytes: bytes,
    tenant_id: UUID,
    workspace_id: UUID | None,
    action: str | None,
    resource_type: str | None,
    requested_limit: int,
) -> dict[str, Any]:
    """Build the tamper-evident manifest for one export bundle."""
    created_values = [event.created_at for event in events]
    return {
        "format": BUNDLE_FORMAT,
        "exported_at": datetime.now(UTC).isoformat(),
        "filters": {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id) if workspace_id is not None else None,
            "action": action,
            "resource_type": resource_type,
            "requested_limit": requested_limit,
            "effective_limit": max(1, min(int(requested_limit), 500)),
        },
        "event_count": len(events),
        "created_at_range": {
            "newest": max(created_values).isoformat() if created_values else None,
            "oldest": min(created_values).isoformat() if created_values else None,
        },
        "files": [
            {
                "path": "audit-events.jsonl",
                "bytes": len(events_bytes),
                "sha256": _sha256(events_bytes),
            }
        ],
        "checksum_algorithm": "sha256",
        "note": (
            "Tamper-evident bundle for recent filtered audit events. "
            "Verify manifest.sha256 first, then audit-events.jsonl sha256."
        ),
    }


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    """Render manifest JSON with stable key ordering."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    """Return a hex SHA-256 digest."""
    return hashlib.sha256(payload).hexdigest()


def _json_ready(value: Any) -> Any:
    """Convert dataclasses, UUIDs and datetimes to JSON-compatible values."""
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_ready(row) for key, row in value.items()}
    if isinstance(value, list | tuple):
        return [_json_ready(row) for row in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
