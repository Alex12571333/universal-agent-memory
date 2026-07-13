"""Validate a retained history of signed encrypted backup bundles.

This is intentionally read-only.  It proves that more than one scheduled run
can still be verified together with its restore-drill and audit evidence; it
does not substitute for keeping the artifacts and signing key independently.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scheduled_backup import BUNDLE_FORMAT, _verify_bundle_manifest

from memory_plane.config.secrets import read_secret_env

REPORT_FORMAT = "obelisk-backup-history-validation-v1"
_MANIFEST_NAME = re.compile(r"^obelisk-memory-(\d{8}T\d{6}Z)\.bundle\.json$")


def main() -> int:
    """Verify retained backup runs and write a machine-readable result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", default=os.getenv("UAM_BACKUP_DIR", "./backups"))
    parser.add_argument("--min-runs", type=int, default=2)
    parser.add_argument(
        "--since",
        help=(
            "Only validate bundle timestamps at or after this UTC boundary, "
            "format YYYYmmddTHHMMSSZ"
        ),
    )
    parser.add_argument(
        "--report",
        default=os.getenv("UAM_BACKUP_HISTORY_REPORT", "./backups/backup-history-report.json"),
    )
    parser.add_argument(
        "--signing-key",
        default=read_secret_env("UAM_BACKUP_SIGNING_KEY"),
        help="HMAC signing key; defaults to UAM_BACKUP_SIGNING_KEY[_FILE]",
    )
    parser.add_argument("--require-signature", action="store_true")
    args = parser.parse_args()
    if args.min_runs < 1:
        parser.error("--min-runs must be positive")
    if args.since and not re.fullmatch(r"\d{8}T\d{6}Z", args.since):
        parser.error("--since must use YYYYmmddTHHMMSSZ")
    if args.require_signature and not args.signing_key:
        parser.error("--require-signature needs UAM_BACKUP_SIGNING_KEY or --signing-key")

    root = Path(args.backup_dir)
    manifests = [
        path
        for path in sorted(root.glob("obelisk-memory-*.bundle.json"))
        if args.since is None or _timestamp_from_name(path) >= args.since
    ]
    checks = [
        _verify_one(
            path,
            signing_key=args.signing_key,
            require_signature=args.require_signature,
        )
        for path in manifests
    ]
    valid = [item for item in checks if item["ok"]]
    report = {
        "format": REPORT_FORMAT,
        "generated_at": datetime.now(UTC).isoformat(),
        "backup_dir": str(root),
        "since": args.since,
        "minimum_runs": args.min_runs,
        "verified_runs": len(valid),
        "ok": len(valid) >= args.min_runs and len(valid) == len(checks),
        "runs": checks,
    }
    target = Path(args.report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"backup_history={'PASS' if report['ok'] else 'FAIL'} report={target}")
    return 0 if report["ok"] else 1


def _timestamp_from_name(path: Path) -> str:
    """Return an already-validated sortable UTC timestamp from a manifest path."""
    matched = _MANIFEST_NAME.match(path.name)
    return matched.group(1) if matched else ""


def _verify_one(path: Path, *, signing_key: str | None, require_signature: bool) -> dict[str, Any]:
    """Validate one complete scheduled-backup bundle without restoring it."""
    name = _MANIFEST_NAME.match(path.name)
    result: dict[str, Any] = {"manifest_path": str(path), "ok": False}
    if name is None:
        result["reason"] = "unexpected bundle manifest filename"
        return result
    timestamp = name.group(1)
    result["timestamp"] = timestamp
    verified, detail = _verify_bundle_manifest(
        path, signing_key=signing_key, require_signature=require_signature
    )
    if not verified:
        result["reason"] = f"bundle verification failed: {detail}"
        return result
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["reason"] = f"cannot parse bundle manifest: {exc}"
        return result
    if manifest.get("format") != BUNDLE_FORMAT:
        result["reason"] = "unexpected backup bundle manifest format"
        return result
    paths = [entry.get("path") for entry in manifest.get("files", []) if isinstance(entry, dict)]
    dump = f"obelisk-memory-{timestamp}.dump.enc"
    restore = f"obelisk-memory-{timestamp}.restore.json"
    has_dump = any(isinstance(item, str) and Path(item).name == dump for item in paths)
    restore_path = next(
        (
            Path(item)
            for item in paths
            if isinstance(item, str) and Path(item).name == restore
        ),
        None,
    )
    has_audit_manifest = any(
        isinstance(item, str) and Path(item).name == "manifest.json" for item in paths
    )
    if not has_dump or restore_path is None or not has_audit_manifest:
        result["reason"] = "bundle is missing encrypted dump, restore drill, or audit manifest"
        return result
    try:
        restore_report = json.loads(restore_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["reason"] = f"cannot read restore drill report: {exc}"
        return result
    if (
        restore_report.get("format") != "obelisk-restore-drill-v1"
        or restore_report.get("ok") is not True
    ):
        result["reason"] = "restore drill report is not successful"
        return result
    result.update({"ok": True, "bundle_verification": detail, "restore_drill": "passed"})
    return result


if __name__ == "__main__":
    raise SystemExit(main())
