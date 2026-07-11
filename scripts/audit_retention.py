"""Export, verify, and optionally prune old audit events."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from memory_plane.adapters.postgres import PostgresMemoryLedger
from memory_plane.api.app import DEFAULT_PROJECT_ID, DEFAULT_SERVER_ID
from memory_plane.config.database import read_database_dsn
from memory_plane.config.secrets import read_secret_env
from memory_plane.services.audit import AuditLogService

ROOT = Path(__file__).resolve().parents[1]
REPORT_FORMAT = "obelisk-audit-retention-v1"


def _retention_database_dsn() -> str | None:
    """Prefer an operator/admin DSN: app roles cannot prune append-only audit."""
    return (
        read_database_dsn(
            "UAM_AUDIT_RETENTION_DATABASE_URL",
            component_prefix="UAM_AUDIT_RETENTION_DATABASE",
        )
        or read_database_dsn(
            "UAM_BACKUP_DATABASE_URL",
            component_prefix="UAM_BACKUP_DATABASE",
        )
        or read_database_dsn(
            "UAM_ADMIN_DATABASE_URL",
            component_prefix="UAM_ADMIN_DATABASE",
        )
        or read_database_dsn()
    )


@dataclass(frozen=True, slots=True)
class AuditRetentionReport:
    """Machine-readable evidence for one audit retention run."""

    format: str
    ok: bool
    dry_run: bool
    tenant_id: str
    workspace_id: str | None
    cutoff: str
    retain_days: int | None
    bundle_dir: str
    exported_event_count: int
    signed_export: bool
    verified_export: bool
    pruned_count: int
    detail: str


def main() -> int:
    """Run a safe audit retention pass."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=_retention_database_dsn(),
        help="Operator/admin PostgreSQL URL; app-role URL is a dry-run fallback only",
    )
    parser.add_argument(
        "--tenant-id",
        type=UUID,
        default=UUID(str(DEFAULT_SERVER_ID)),
        help="Tenant/server UUID to retain",
    )
    parser.add_argument(
        "--workspace-id",
        type=UUID,
        default=UUID(str(DEFAULT_PROJECT_ID)),
        help="Workspace/project UUID filter",
    )
    parser.add_argument("--all-workspaces", action="store_true")
    parser.add_argument(
        "--retain-days",
        type=int,
        default=int(read_secret_env("UAM_AUDIT_RETENTION_DAYS") or "365"),
        help="Keep events newer than this many days; ignored when --cutoff is set",
    )
    parser.add_argument("--cutoff", help="Exclusive ISO-8601 created_at cutoff")
    parser.add_argument(
        "--export-root",
        type=Path,
        default=Path(read_secret_env("UAM_AUDIT_RETENTION_EXPORT_DIR") or "./audit-retention"),
    )
    parser.add_argument(
        "--signing-key",
        default=read_secret_env("UAM_AUDIT_SIGNING_KEY"),
        help="HMAC signing key for the mandatory pre-prune export",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-delete-batches", type=int, default=100)
    parser.add_argument("--json-report", type=Path)
    parser.add_argument("--apply", action="store_true", help="Actually delete exported events")
    parser.add_argument(
        "--allow-unsigned-export",
        action="store_true",
        help="Permit --apply without a signing key; not recommended for production",
    )
    args = parser.parse_args()

    if not args.database_url:
        parser.error("database URL is required")
    if args.apply and not args.signing_key and not args.allow_unsigned_export:
        parser.error("--apply requires --signing-key or --allow-unsigned-export")
    if args.retain_days is not None and args.retain_days < 1:
        parser.error("--retain-days must be positive")

    cutoff = _cutoff(args.cutoff, args.retain_days)
    workspace_id = None if args.all_workspaces else args.workspace_id
    bundle_dir = args.export_root / cutoff.strftime("%Y%m%dT%H%M%SZ")
    bundle_dir.mkdir(parents=True, exist_ok=True)

    export_ok = _run_export(
        bundle_dir,
        database_url=args.database_url,
        tenant_id=args.tenant_id,
        workspace_id=workspace_id,
        cutoff=cutoff,
        batch_size=args.batch_size,
        signing_key=args.signing_key,
    )
    if not export_ok:
        report = _report(
            ok=False,
            dry_run=not args.apply,
            tenant_id=args.tenant_id,
            workspace_id=workspace_id,
            cutoff=cutoff,
            retain_days=args.retain_days,
            bundle_dir=bundle_dir,
            exported_event_count=0,
            signed_export=bool(args.signing_key),
            verified_export=False,
            pruned_count=0,
            detail="audit export failed",
        )
        _emit(report, args.json_report)
        return 1

    verified = _run_verify(bundle_dir, signing_key=args.signing_key)
    manifest = _read_manifest(bundle_dir)
    exported_count = int(manifest.get("event_count") or 0)
    if not verified:
        report = _report(
            ok=False,
            dry_run=not args.apply,
            tenant_id=args.tenant_id,
            workspace_id=workspace_id,
            cutoff=cutoff,
            retain_days=args.retain_days,
            bundle_dir=bundle_dir,
            exported_event_count=exported_count,
            signed_export=bool(args.signing_key),
            verified_export=False,
            pruned_count=0,
            detail="audit export verification failed",
        )
        _emit(report, args.json_report)
        return 1

    pruned_count = 0
    if args.apply and exported_count:
        ledger = PostgresMemoryLedger(args.database_url)
        ledger.connect()
        audit = AuditLogService(ledger)
        for _ in range(max(1, args.max_delete_batches)):
            deleted = audit.prune_events(
                args.tenant_id,
                workspace_id=workspace_id,
                created_before=cutoff,
                limit=args.batch_size,
            )
            pruned_count += deleted
            if deleted < max(1, min(args.batch_size, 500)):
                break

    report = _report(
        ok=True,
        dry_run=not args.apply,
        tenant_id=args.tenant_id,
        workspace_id=workspace_id,
        cutoff=cutoff,
        retain_days=args.retain_days,
        bundle_dir=bundle_dir,
        exported_event_count=exported_count,
        signed_export=bool(args.signing_key),
        verified_export=True,
        pruned_count=pruned_count,
        detail="dry-run complete" if not args.apply else "retention applied",
    )
    _emit(report, args.json_report)
    return 0


def _run_export(
    bundle_dir: Path,
    *,
    database_url: str,
    tenant_id: UUID,
    workspace_id: UUID | None,
    cutoff: datetime,
    batch_size: int,
    signing_key: str | None,
) -> bool:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "export_audit.py"),
        str(bundle_dir),
        "--database-url",
        database_url,
        "--tenant-id",
        str(tenant_id),
        "--until",
        cutoff.isoformat(),
        "--all-pages",
        "--batch-size",
        str(batch_size),
    ]
    if workspace_id is None:
        command.append("--all-workspaces")
    else:
        command.extend(["--workspace-id", str(workspace_id)])
    if signing_key:
        command.extend(["--signing-key", signing_key])
    return subprocess.run(command, check=False).returncode == 0


def _run_verify(bundle_dir: Path, *, signing_key: str | None) -> bool:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "export_audit.py"),
        str(bundle_dir),
        "--verify",
    ]
    if signing_key:
        command.extend(["--signing-key", signing_key])
    return subprocess.run(command, check=False).returncode == 0


def _read_manifest(bundle_dir: Path) -> dict[str, object]:
    try:
        data = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _cutoff(value: str | None, retain_days: int | None) -> datetime:
    if value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    days = 365 if retain_days is None else retain_days
    return datetime.now(UTC) - timedelta(days=days)


def _report(
    *,
    ok: bool,
    dry_run: bool,
    tenant_id: UUID,
    workspace_id: UUID | None,
    cutoff: datetime,
    retain_days: int | None,
    bundle_dir: Path,
    exported_event_count: int,
    signed_export: bool,
    verified_export: bool,
    pruned_count: int,
    detail: str,
) -> AuditRetentionReport:
    return AuditRetentionReport(
        format=REPORT_FORMAT,
        ok=ok,
        dry_run=dry_run,
        tenant_id=str(tenant_id),
        workspace_id=str(workspace_id) if workspace_id is not None else None,
        cutoff=cutoff.isoformat(),
        retain_days=retain_days,
        bundle_dir=str(bundle_dir),
        exported_event_count=exported_event_count,
        signed_export=signed_export,
        verified_export=verified_export,
        pruned_count=pruned_count,
        detail=detail,
    )


def _emit(report: AuditRetentionReport, json_report: Path | None) -> None:
    payload = json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n"
    if json_report is not None:
        json_report.parent.mkdir(parents=True, exist_ok=True)
        json_report.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    raise SystemExit(main())
