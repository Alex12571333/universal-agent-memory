"""Run scheduled backup, restore drill, optional audit export, and alerting."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


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
        default=os.getenv("UAM_BACKUP_DATABASE_URL")
        or os.getenv("UAM_ADMIN_DATABASE_URL")
        or os.getenv("UAM_DATABASE_URL"),
        help="PostgreSQL URL passed to backup.py",
    )
    parser.add_argument(
        "--alert-webhook",
        default=os.getenv("UAM_BACKUP_ALERT_WEBHOOK"),
        help="Optional HTTP webhook called when the job fails",
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

    timestamp = args.timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(args.backup_dir)
    audit_root = Path(args.audit_dir)
    report_path = Path(args.report)
    backup_path = backup_dir / f"obelisk-memory-{timestamp}.dump"
    audit_path = audit_root / timestamp
    started = time.time()
    steps: list[dict[str, Any]] = []

    backup_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if not args.skip_audit_export:
        audit_path.mkdir(parents=True, exist_ok=True)

    success = True
    try:
        success &= _run_step(
            steps,
            "backup",
            [
                sys.executable,
                str(ROOT / "scripts" / "backup.py"),
                str(backup_path),
                "--database-url",
                args.database_url,
            ],
        )
        if not args.skip_restore_drill and steps[-1]["ok"]:
            success &= _run_step(
                steps,
                "restore_drill",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "restore_drill.py"),
                    str(backup_path),
                ],
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
                    "--database-url",
                    args.database_url,
                    "--limit",
                    "500",
                ],
            )
        else:
            steps.append(_skipped_step("audit_export", "skipped by operator"))
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

    report = {
        "format": "obelisk-scheduled-backup-report-v1",
        "ok": success,
        "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.time() - started, 3),
        "backup_path": str(backup_path),
        "audit_export_path": None if args.skip_audit_export else str(audit_path),
        "restore_drill_required": not args.skip_restore_drill,
        "steps": steps,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    if not success and args.alert_webhook:
        _send_alert(args.alert_webhook, report)
    print(f"scheduled_backup={'PASS' if success else 'FAIL'} report={report_path}")
    return 0 if success else 1


def _run_step(
    steps: list[dict[str, Any]],
    name: str,
    command: list[str],
) -> bool:
    """Run one subprocess step and append a compact report entry."""
    started = time.time()
    result = subprocess.run(command, check=False, text=True, capture_output=True)
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
    payload = json.dumps(report, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "obelisk-memory-scheduled-backup",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except urllib.error.URLError as exc:
        print(f"backup_alert=FAIL reason={exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
