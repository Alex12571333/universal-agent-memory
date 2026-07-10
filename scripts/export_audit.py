"""Export recent audit events as a tamper-evident forensic bundle."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from memory_plane.adapters.postgres import PostgresMemoryLedger
from memory_plane.api.app import DEFAULT_PROJECT_ID, DEFAULT_SERVER_ID
from memory_plane.config.database import read_database_dsn
from memory_plane.config.secrets import read_secret_env
from memory_plane.domain.audit import AuditEvent
from memory_plane.services.audit import AuditLogService

BUNDLE_FORMAT = "obelisk-audit-export-v1"


def main() -> int:
    """Export operator audit events into JSONL plus checksum manifest files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", help="Directory where audit bundle files are written")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify an existing bundle instead of exporting from PostgreSQL",
    )
    parser.add_argument(
        "--database-url",
        default=read_database_dsn(),
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
    parser.add_argument("--since", help="Inclusive ISO-8601 lower created_at bound")
    parser.add_argument("--until", help="Exclusive ISO-8601 upper created_at bound")
    parser.add_argument(
        "--all-pages",
        action="store_true",
        help="Export every page in the selected time/filter window",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Page size for --all-pages; repository cap is 500",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum events to export; repository cap is 500",
    )
    parser.add_argument(
        "--signing-key",
        default=read_secret_env("UAM_AUDIT_SIGNING_KEY"),
        help="Optional HMAC signing key; defaults to UAM_AUDIT_SIGNING_KEY",
    )
    args = parser.parse_args()
    if args.verify:
        return _verify_bundle(Path(args.output_dir), signing_key=args.signing_key)
    if not args.database_url:
        parser.error("database URL is required")

    ledger = PostgresMemoryLedger(args.database_url)
    ledger.connect()
    audit = AuditLogService(ledger)
    workspace_id = None if args.all_workspaces else args.workspace_id
    created_after = _parse_datetime(args.since)
    created_before = _parse_datetime(args.until)
    events, page_count = _collect_events(
        audit,
        tenant_id=args.tenant_id,
        workspace_id=workspace_id,
        action=args.action,
        resource_type=args.resource_type,
        created_after=created_after,
        created_before=created_before,
        limit=args.limit,
        batch_size=args.batch_size,
        all_pages=args.all_pages,
    )

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    events_path = output / "audit-events.jsonl"
    manifest_path = output / "manifest.json"
    manifest_checksum_path = output / "manifest.sha256"
    manifest_signature_path = output / "manifest.sig"

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
        created_after=created_after,
        created_before=created_before,
        all_pages=args.all_pages,
        batch_size=args.batch_size,
        page_count=page_count,
        signed=bool(args.signing_key),
    )
    manifest_bytes = _canonical_json_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    manifest_checksum_path.write_text(
        f"{_sha256(manifest_bytes)}  manifest.json\n",
        encoding="utf-8",
    )
    if args.signing_key:
        manifest_signature_path.write_text(
            f"{_hmac_sha256(args.signing_key, manifest_bytes)}  manifest.json\n",
            encoding="utf-8",
        )

    print(
        "audit_export=PASS "
        f"events={len(events)} pages={page_count} output={output} "
        f"manifest_sha256={_sha256(manifest_bytes)} "
        f"signed={'yes' if args.signing_key else 'no'}"
    )
    return 0


def _collect_events(
    audit: AuditLogService,
    *,
    tenant_id: UUID,
    workspace_id: UUID | None,
    action: str | None,
    resource_type: str | None,
    created_after: datetime | None,
    created_before: datetime | None,
    limit: int,
    batch_size: int,
    all_pages: bool,
) -> tuple[tuple[AuditEvent, ...], int]:
    """Collect one or more cursor pages from the audit service."""
    if not all_pages:
        return (
            audit.list_events(
                tenant_id,
                workspace_id=workspace_id,
                action=action,
                resource_type=resource_type,
                created_after=created_after,
                created_before=created_before,
                limit=limit,
            ),
            1,
        )

    page_limit = max(1, min(int(batch_size), 500))
    page_count = 0
    cursor_created_before = created_before
    cursor_before_event_id: UUID | None = None
    rows: list[AuditEvent] = []
    while True:
        page = audit.list_events(
            tenant_id,
            workspace_id=workspace_id,
            action=action,
            resource_type=resource_type,
            created_after=created_after,
            created_before=cursor_created_before,
            before_event_id=cursor_before_event_id,
            limit=page_limit,
        )
        page_count += 1
        rows.extend(page)
        if len(page) < page_limit:
            break
        last = page[-1]
        cursor_created_before = last.created_at
        cursor_before_event_id = last.id
    return tuple(rows), page_count


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
    created_after: datetime | None,
    created_before: datetime | None,
    all_pages: bool,
    batch_size: int,
    page_count: int,
    signed: bool,
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
            "created_after": created_after.isoformat() if created_after else None,
            "created_before": created_before.isoformat() if created_before else None,
            "all_pages": all_pages,
            "batch_size": max(1, min(int(batch_size), 500)),
            "page_count": page_count,
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
        "signature_algorithm": "hmac-sha256" if signed else None,
        "note": (
            "Tamper-evident bundle for recent filtered audit events. "
            "Verify manifest.sha256, audit-events.jsonl sha256 and manifest.sig when signed."
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


def _hmac_sha256(secret: str, payload: bytes) -> str:
    """Return a hex HMAC-SHA256 signature for one payload."""
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse ISO-8601 datetimes, accepting a trailing Z."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _verify_bundle(output: Path, *, signing_key: str | None) -> int:
    """Verify checksum and optional signature files for one audit bundle."""
    manifest_path = output / "manifest.json"
    checksum_path = output / "manifest.sha256"
    signature_path = output / "manifest.sig"
    events_path = output / "audit-events.jsonl"
    checks: list[dict[str, Any]] = []

    manifest_bytes = _read_required(manifest_path, checks)
    expected_manifest_sha = _read_digest(checksum_path, checks)
    if manifest_bytes is not None and expected_manifest_sha is not None:
        actual = _sha256(manifest_bytes)
        checks.append(
            {
                "name": "manifest.sha256",
                "ok": hmac.compare_digest(actual, expected_manifest_sha),
                "expected": expected_manifest_sha,
                "actual": actual,
            }
        )

    manifest: dict[str, Any] | None = None
    if manifest_bytes is not None:
        try:
            parsed = json.loads(manifest_bytes.decode("utf-8"))
            manifest = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError as exc:
            checks.append({"name": "manifest.json", "ok": False, "error": str(exc)})

    events_bytes = _read_required(events_path, checks)
    if manifest is not None and events_bytes is not None:
        files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
        event_meta = next(
            (
                row
                for row in files
                if isinstance(row, dict) and row.get("path") == "audit-events.jsonl"
            ),
            {},
        )
        expected_events_sha = str(event_meta.get("sha256") or "")
        actual_events_sha = _sha256(events_bytes)
        checks.append(
            {
                "name": "audit-events.jsonl.sha256",
                "ok": bool(expected_events_sha)
                and hmac.compare_digest(actual_events_sha, expected_events_sha),
                "expected": expected_events_sha,
                "actual": actual_events_sha,
            }
        )

    if signing_key:
        expected_signature = _read_digest(signature_path, checks)
        if manifest_bytes is not None and expected_signature is not None:
            actual_signature = _hmac_sha256(signing_key, manifest_bytes)
            checks.append(
                {
                    "name": "manifest.sig",
                    "ok": hmac.compare_digest(actual_signature, expected_signature),
                    "expected": expected_signature,
                    "actual": actual_signature,
                }
            )
    else:
        checks.append(
            {
                "name": "manifest.sig",
                "ok": not signature_path.exists(),
                "detail": (
                    "unsigned bundle"
                    if not signature_path.exists()
                    else "signing key required"
                ),
            }
        )

    ok = all(check.get("ok") is True for check in checks)
    print(
        json.dumps(
            {
                "format": "obelisk-audit-export-verification-v1",
                "ok": ok,
                "bundle": str(output),
                "checks": checks,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if ok else 1


def _read_required(path: Path, checks: list[dict[str, Any]]) -> bytes | None:
    """Read a required file and append a failed check when it is missing."""
    try:
        return path.read_bytes()
    except OSError as exc:
        checks.append({"name": path.name, "ok": False, "error": str(exc)})
        return None


def _read_digest(path: Path, checks: list[dict[str, Any]]) -> str | None:
    """Read the first digest field from a sha/signature file."""
    try:
        return path.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError) as exc:
        checks.append({"name": path.name, "ok": False, "error": str(exc)})
        return None


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
