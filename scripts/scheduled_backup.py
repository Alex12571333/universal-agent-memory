"""Run scheduled backup, restore drill, optional audit export, and alerting."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backup_encryption import BackupEncryptionError, encrypt_file, key_fingerprint, parse_key

from memory_plane.config.database import read_database_dsn
from memory_plane.config.secrets import read_secret_env
from memory_plane.services.alerting import send_alert

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_FORMAT = "obelisk-backup-bundle-v1"


def main() -> int:
    """Execute the production backup job and write a machine-readable report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup-dir",
        default=os.getenv("UAM_BACKUP_DIR", "./backups"),
        help="Directory for generated PostgreSQL dumps",
    )
    parser.add_argument(
        "--audit-dir",
        default=os.getenv("UAM_AUDIT_EXPORT_DIR", "./audit-exports"),
        help="Directory for audit export bundles",
    )
    parser.add_argument(
        "--report",
        default=os.getenv("UAM_BACKUP_REPORT", "./backups/latest-backup-report.json"),
        help="JSON report path",
    )
    parser.add_argument(
        "--database-url",
        default=(
            read_database_dsn(
                "UAM_BACKUP_DATABASE_URL",
                component_prefix="UAM_BACKUP_DATABASE",
            )
            or read_database_dsn(
                "UAM_ADMIN_DATABASE_URL",
                component_prefix="UAM_ADMIN_DATABASE",
            )
            or read_database_dsn()
        ),
        help="PostgreSQL URL passed to backup.py",
    )
    parser.add_argument(
        "--alert-webhook",
        default=read_secret_env("UAM_BACKUP_ALERT_WEBHOOK"),
        help="Optional HTTP webhook called when the job fails",
    )
    parser.add_argument(
        "--alert-command",
        default=os.getenv("UAM_ALERT_COMMAND", ""),
        help="Optional local command receiving the JSON failure report on stdin",
    )
    parser.add_argument(
        "--encryption-key",
        default=read_secret_env("UAM_BACKUP_ENCRYPTION_KEY"),
        help="URL-safe base64 AES-256 backup key; defaults to UAM_BACKUP_ENCRYPTION_KEY[_FILE]",
    )
    parser.add_argument(
        "--signing-key",
        default=read_secret_env("UAM_BACKUP_SIGNING_KEY"),
        help="Optional HMAC key for a signed encrypted-backup bundle manifest",
    )
    parser.add_argument(
        "--signing-key-id",
        default=os.getenv("UAM_BACKUP_SIGNING_KEY_ID", ""),
        help="Non-secret identifier for --signing-key",
    )
    parser.add_argument(
        "--require-signature",
        action="store_true",
        help="Fail the job unless an encrypted-backup bundle manifest is signed",
    )
    parser.add_argument(
        "--skip-audit-export",
        action="store_true",
        help="Skip audit bundle export",
    )
    parser.add_argument(
        "--skip-restore-drill",
        action="store_true",
        help="Skip restore drill; not allowed for production release evidence",
    )
    parser.add_argument(
        "--timestamp",
        help="Stable timestamp override for tests, format YYYYmmddTHHMMSSZ",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("database URL is required")
    if not args.encryption_key:
        parser.error("UAM_BACKUP_ENCRYPTION_KEY or --encryption-key is required")
    if args.require_signature and not args.signing_key:
        parser.error("--require-signature needs UAM_BACKUP_SIGNING_KEY or --signing-key")
    if args.signing_key and not args.signing_key_id.strip():
        parser.error("--signing-key requires --signing-key-id or UAM_BACKUP_SIGNING_KEY_ID")
    try:
        encryption_key = parse_key(args.encryption_key)
    except BackupEncryptionError as exc:
        parser.error(str(exc))

    timestamp = args.timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(args.backup_dir)
    audit_root = Path(args.audit_dir)
    report_path = Path(args.report)
    backup_path = backup_dir / f"obelisk-memory-{timestamp}.dump.enc"
    restore_report_path = backup_dir / f"obelisk-memory-{timestamp}.restore.json"
    audit_path = audit_root / timestamp
    bundle_manifest_path = backup_dir / f"obelisk-memory-{timestamp}.bundle.json"
    started = time.time()
    steps: list[dict[str, Any]] = []

    backup_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if not args.skip_audit_export:
        audit_path.mkdir(parents=True, exist_ok=True)

    success = True
    plaintext_backup: Path | None = None
    child_environment = os.environ.copy()
    child_environment["UAM_BACKUP_DATABASE_URL"] = args.database_url
    child_environment["UAM_DATABASE_URL"] = args.database_url
    child_environment["UAM_BACKUP_ENCRYPTION_KEY"] = args.encryption_key
    try:
        descriptor, name = tempfile.mkstemp(prefix="obelisk-backup-", suffix=".dump")
        os.close(descriptor)
        plaintext_backup = Path(name)
        plaintext_backup.chmod(0o600)
        success &= _run_step(
            steps,
            "backup",
            [
                sys.executable,
                str(ROOT / "scripts" / "backup.py"),
                str(plaintext_backup),
            ],
            environment=child_environment,
        )
        if steps[-1]["ok"]:
            success &= _encrypt_step(steps, plaintext_backup, backup_path, encryption_key)
        if not args.skip_restore_drill and steps[-1]["ok"]:
            success &= _run_step(
                steps,
                "restore_drill",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "restore_drill.py"),
                    str(backup_path),
                    "--report",
                    str(restore_report_path),
                ],
                environment=child_environment,
            )
        elif args.skip_restore_drill:
            steps.append(_skipped_step("restore_drill", "skipped by operator"))

        if not args.skip_audit_export:
            success &= _run_step(
                steps,
                "audit_export",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "export_audit.py"),
                    str(audit_path),
                    "--limit",
                    "500",
                ],
                environment=child_environment,
            )
        else:
            steps.append(_skipped_step("audit_export", "skipped by operator"))
        if steps and all(step.get("ok") is True for step in steps):
            success &= _write_bundle_manifest_step(
                steps,
                backup_path=backup_path,
                restore_report_path=(None if args.skip_restore_drill else restore_report_path),
                audit_path=None if args.skip_audit_export else audit_path,
                target=bundle_manifest_path,
                timestamp=timestamp,
                signing_key=args.signing_key,
                signing_key_id=args.signing_key_id,
                require_signature=args.require_signature,
            )
        else:
            steps.append(
                _skipped_step("backup_bundle_manifest", "a prerequisite backup step failed")
            )
    except Exception as exc:  # pragma: no cover - defensive safety net
        success = False
        steps.append(
            {
                "name": "scheduled_backup",
                "ok": False,
                "returncode": None,
                "duration_seconds": 0.0,
                "stdout": "",
                "stderr": str(exc),
            }
        )
    finally:
        if plaintext_backup is not None:
            plaintext_backup.unlink(missing_ok=True)

    report = {
        "format": "obelisk-scheduled-backup-report-v2",
        "ok": success,
        "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.time() - started, 3),
        "backup_path": str(backup_path),
        "restore_drill_report_path": (
            str(restore_report_path)
            if not args.skip_restore_drill and restore_report_path.is_file()
            else None
        ),
        "backup_encryption": {
            "algorithm": "AES-256-GCM",
            "key_fingerprint": key_fingerprint(encryption_key),
        },
        "audit_export_path": None if args.skip_audit_export else str(audit_path),
        "bundle_manifest_path": str(bundle_manifest_path),
        "bundle_signed": bool(args.signing_key),
        "restore_drill_required": not args.skip_restore_drill,
        "steps": steps,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    if not success and args.alert_webhook:
        _send_alert(args.alert_webhook, report)
    if not success and args.alert_command:
        send_alert(report, command=args.alert_command, user_agent="obelisk-memory-scheduled-backup")
    print(f"scheduled_backup={'PASS' if success else 'FAIL'} report={report_path}")
    return 0 if success else 1


def _run_step(
    steps: list[dict[str, Any]],
    name: str,
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
) -> bool:
    """Run one subprocess step and append a compact report entry."""
    started = time.time()
    result = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
        env=environment,
    )
    steps.append(
        {
            "name": name,
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "duration_seconds": round(time.time() - started, 3),
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        }
    )
    return result.returncode == 0


def _encrypt_step(steps: list[dict[str, Any]], source: Path, target: Path, key: bytes) -> bool:
    """Encrypt a temporary dump without placing a key in a subprocess command."""
    started = time.time()
    try:
        metadata = encrypt_file(source, target, key)
    except Exception as exc:
        steps.append(
            {
                "name": "backup_encryption",
                "ok": False,
                "returncode": None,
                "duration_seconds": round(time.time() - started, 3),
                "stdout": "",
                "stderr": str(exc),
            }
        )
        return False
    steps.append(
        {
            "name": "backup_encryption",
            "ok": True,
            "returncode": 0,
            "duration_seconds": round(time.time() - started, 3),
            "stdout": json.dumps(metadata, sort_keys=True),
            "stderr": "",
        }
    )
    return True


def _write_bundle_manifest_step(
    steps: list[dict[str, Any]],
    *,
    backup_path: Path,
    restore_report_path: Path | None,
    audit_path: Path | None,
    target: Path,
    timestamp: str,
    signing_key: str | None,
    signing_key_id: str,
    require_signature: bool,
) -> bool:
    """Hash the encrypted backup and optional audit bundle into one manifest."""
    started = time.time()
    try:
        entries = [_bundle_file_entry(backup_path)]
        if restore_report_path is not None:
            if not restore_report_path.is_file():
                raise FileNotFoundError(f"restore drill report not found: {restore_report_path}")
            entries.append(_bundle_file_entry(restore_report_path))
        if audit_path is not None:
            audit_manifest = audit_path / "manifest.json"
            if not audit_manifest.is_file():
                raise FileNotFoundError(f"audit manifest not found: {audit_manifest}")
            entries.append(_bundle_file_entry(audit_manifest))
        manifest: dict[str, Any] = {
            "format": BUNDLE_FORMAT,
            "created_at": datetime.now(UTC).isoformat(),
            "timestamp": timestamp,
            "files": entries,
            "signature": None,
        }
        if signing_key:
            signature = _sign_bundle_manifest(manifest, signing_key)
            manifest["signature"] = {
                "algorithm": "hmac-sha256",
                "key_id": signing_key_id.strip(),
                "value": signature,
            }
        elif require_signature:
            raise ValueError("backup bundle signature is required")
        target.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        verified, detail = _verify_bundle_manifest(
            target,
            signing_key=signing_key,
            require_signature=require_signature,
        )
        if not verified:
            raise ValueError(f"backup bundle verification failed: {detail}")
        steps.append(
            {
                "name": "backup_bundle_manifest",
                "ok": True,
                "returncode": 0,
                "duration_seconds": round(time.time() - started, 3),
                "stdout": json.dumps(
                    {
                        "path": str(target),
                        "signed": bool(signing_key),
                        "file_count": len(entries),
                        "verified": verified,
                    },
                    sort_keys=True,
                ),
                "stderr": "",
            }
        )
        return True
    except Exception as exc:
        steps.append(
            {
                "name": "backup_bundle_manifest",
                "ok": False,
                "returncode": None,
                "duration_seconds": round(time.time() - started, 3),
                "stdout": "",
                "stderr": str(exc),
            }
        )
        return False


def _bundle_file_entry(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "path": str(path),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _sign_bundle_manifest(manifest: dict[str, Any], signing_key: str) -> str:
    unsigned = dict(manifest)
    unsigned["signature"] = None
    payload = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(signing_key.encode(), payload, hashlib.sha256).hexdigest()


def _verify_bundle_manifest(
    path: Path,
    *,
    signing_key: str | None,
    require_signature: bool,
) -> tuple[bool, str]:
    """Verify bundle shape, file hashes, and optional HMAC before hand-off."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"cannot read manifest: {exc}"
    if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT:
        return False, "unexpected backup bundle manifest format"
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return False, "bundle manifest has no files"
    for entry in files:
        if not isinstance(entry, dict):
            return False, "bundle manifest contains invalid file entry"
        raw_path = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(expected, str):
            return False, "bundle manifest file entry is incomplete"
        try:
            actual = hashlib.sha256(Path(raw_path).read_bytes()).hexdigest()
        except OSError as exc:
            return False, f"cannot read bundle file {raw_path}: {exc}"
        if not hmac.compare_digest(actual, expected):
            return False, f"digest mismatch for bundle file {raw_path}"
    signature = manifest.get("signature")
    if signature is None:
        return (False, "bundle manifest is unsigned") if require_signature else (True, "unsigned")
    if not isinstance(signature, dict) or signature.get("algorithm") != "hmac-sha256":
        return False, "unsupported bundle signature"
    if not signing_key:
        return False, "signing key is required to verify bundle signature"
    expected_signature = signature.get("value")
    if not isinstance(expected_signature, str):
        return False, "bundle signature is missing"
    actual_signature = _sign_bundle_manifest(manifest, signing_key)
    if not hmac.compare_digest(actual_signature, expected_signature):
        return False, "bundle signature mismatch"
    return True, "signed"


def _skipped_step(name: str, reason: str) -> dict[str, Any]:
    """Return a report row for an intentionally skipped step."""
    return {
        "name": name,
        "ok": True,
        "skipped": True,
        "returncode": None,
        "duration_seconds": 0.0,
        "stdout": reason,
        "stderr": "",
    }


def _send_alert(webhook: str, report: dict[str, Any]) -> None:
    """Send a best-effort JSON alert for failed scheduled backup jobs."""
    send_alert(report, webhook=webhook, user_agent="obelisk-memory-scheduled-backup")


if __name__ == "__main__":
    raise SystemExit(main())
