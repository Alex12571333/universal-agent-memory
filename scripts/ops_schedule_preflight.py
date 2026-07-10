"""Verify installed operations schedules, alert routing, and artifact storage."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from validate_production_env import parse_env_file

REPORT_FORMAT = "obelisk-ops-schedule-preflight-v1"
DEFAULT_ALLOWED_ARTIFACT_PREFIXES = (
    "s3://",
    "gs://",
    "az://",
    "/mnt/immutable",
    "/var/backups/obelisk",
)


def main() -> int:
    """Check ops schedule evidence and optionally write a JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", type=Path, help="Production dotenv file to inspect.")
    parser.add_argument("--backup-schedule-file", type=Path, required=True)
    parser.add_argument("--audit-retention-schedule-file", type=Path, required=True)
    parser.add_argument("--metrics-schedule-file", type=Path, required=True)
    parser.add_argument("--backup-artifact-root", required=True)
    parser.add_argument("--audit-artifact-root", required=True)
    parser.add_argument(
        "--allowed-artifact-prefix",
        action="append",
        default=[],
        help="Allowed durable/immutable artifact URI/path prefix.",
    )
    parser.add_argument("--report", type=Path, help="Write JSON release evidence.")
    args = parser.parse_args()

    report = run_preflight(
        env_file=args.env_file,
        backup_schedule_file=args.backup_schedule_file,
        audit_retention_schedule_file=args.audit_retention_schedule_file,
        metrics_schedule_file=args.metrics_schedule_file,
        backup_artifact_root=args.backup_artifact_root,
        audit_artifact_root=args.audit_artifact_root,
        allowed_artifact_prefixes=tuple(args.allowed_artifact_prefix)
        or DEFAULT_ALLOWED_ARTIFACT_PREFIXES,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


def run_preflight(
    *,
    env_file: Path,
    backup_schedule_file: Path,
    audit_retention_schedule_file: Path,
    metrics_schedule_file: Path,
    backup_artifact_root: str,
    audit_artifact_root: str,
    allowed_artifact_prefixes: tuple[str, ...] = DEFAULT_ALLOWED_ARTIFACT_PREFIXES,
) -> dict[str, Any]:
    """Return machine-readable ops installation evidence."""
    values = parse_env_file(env_file)
    checks = [
        *_schedule_checks(
            "backup-schedule",
            backup_schedule_file,
            ("scheduled_backup.py", "--report"),
        ),
        *_schedule_checks(
            "audit-retention-schedule",
            audit_retention_schedule_file,
            ("audit_retention.py", "--json-report", "--apply"),
        ),
        *_schedule_checks(
            "metrics-schedule",
            metrics_schedule_file,
            ("check_metrics_health.py", "--report"),
        ),
        _configured_check(values, "UAM_BACKUP_ALERT_WEBHOOK"),
        _configured_check(values, "UAM_METRICS_ALERT_WEBHOOK"),
        _artifact_root_check(
            "backup-artifact-root",
            backup_artifact_root,
            allowed_artifact_prefixes,
        ),
        _artifact_root_check(
            "audit-artifact-root",
            audit_artifact_root,
            allowed_artifact_prefixes,
        ),
    ]
    return {
        "format": REPORT_FORMAT,
        "ok": all(check["ok"] for check in checks),
        "checked_at": datetime.now(UTC).isoformat(),
        "env_file": str(env_file),
        "backup_schedule_file": str(backup_schedule_file),
        "audit_retention_schedule_file": str(audit_retention_schedule_file),
        "metrics_schedule_file": str(metrics_schedule_file),
        "backup_artifact_root": backup_artifact_root,
        "audit_artifact_root": audit_artifact_root,
        "allowed_artifact_prefixes": list(allowed_artifact_prefixes),
        "checks": checks,
    }


def _schedule_checks(
    name: str,
    path: Path,
    required_tokens: tuple[str, ...],
) -> list[dict[str, Any]]:
    if not path.exists():
        return [
            {
                "name": f"{name}:file-exists",
                "ok": False,
                "detail": f"{path} missing",
            },
            {
                "name": f"{name}:required-command",
                "ok": False,
                "detail": "schedule file missing",
            },
        ]
    text = path.read_text(encoding="utf-8")
    missing = [token for token in required_tokens if token not in text]
    return [
        {
            "name": f"{name}:file-exists",
            "ok": True,
            "detail": f"{path} exists",
        },
        {
            "name": f"{name}:required-command",
            "ok": not missing,
            "detail": (
                "required command present" if not missing else "missing: " + ", ".join(missing)
            ),
        },
    ]


def _configured_check(values: dict[str, str], key: str) -> dict[str, Any]:
    configured = bool(values.get(key, "").strip() or values.get(f"{key}_FILE", "").strip())
    return {
        "name": f"{key}:configured",
        "ok": configured,
        "detail": f"{key} or {key}_FILE configured" if configured else f"{key} missing",
    }


def _artifact_root_check(
    name: str,
    value: str,
    allowed_prefixes: tuple[str, ...],
) -> dict[str, Any]:
    root = value.strip()
    ok = bool(root) and any(root.startswith(prefix) for prefix in allowed_prefixes)
    return {
        "name": f"{name}:durable-prefix",
        "ok": ok,
        "detail": (
            "artifact root uses an allowed durable prefix"
            if ok
            else f"{root or '<empty>'} is outside allowed prefixes"
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
