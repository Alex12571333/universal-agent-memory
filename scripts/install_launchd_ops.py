"""Install local macOS launchd jobs for an Obelisk self-hosted appliance.

The jobs run in the logged-in user's launchd domain and read secrets only from
an operator-owned mode-0600 env file.  No public URL, VPN, reverse proxy, or
webhook is required.
"""

from __future__ import annotations

import argparse
import plistlib
from pathlib import Path

LABEL_PREFIX = "com.obelisk-memory"


def _job(
    label: str, script: Path, hour: int, minute: int, weekday: int | None = None
) -> dict[object, object]:
    interval: dict[str, int] = {"Hour": hour, "Minute": minute}
    if weekday is not None:
        interval["Weekday"] = weekday
    return {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", str(script)],
        "StartCalendarInterval": interval,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(script.with_suffix(".out.log")),
        "StandardErrorPath": str(script.with_suffix(".err.log")),
    }


def _wrapper(workspace: Path, env_file: Path, task: str) -> str:
    commands = {
        "backup": (
            '"$OBELISK_PYTHON" scripts/scheduled_backup.py '
            '--backup-dir "$OBELISK_BACKUP_DIR" --audit-dir "$OBELISK_AUDIT_DIR" '
            '--report "$OBELISK_EVIDENCE_DIR/latest-backup-report.json" '
            '--require-signature'
        ),
        "maintenance": (
            '"$OBELISK_PYTHON" scripts/maintenance_retention.py '
            '--apply --report "$OBELISK_EVIDENCE_DIR/maintenance-retention.json"'
        ),
        "audit-retention": (
            '"$OBELISK_PYTHON" scripts/audit_retention.py '
            '--tenant-id "$UAM_SERVER_ID" --workspace-id "$UAM_PROJECT_ID" '
            '--export-root "${OBELISK_AUDIT_RETENTION_DIR:-$OBELISK_AUDIT_DIR/retention}" '
            '--json-report "$OBELISK_EVIDENCE_DIR/audit-retention.json" --apply'
        ),
        "metrics": (
            '"$OBELISK_PYTHON" scripts/check_metrics_health.py '
            '--metrics-url "${UAM_METRICS_URL:-http://127.0.0.1:6798/metrics}" '
            '--report "$OBELISK_EVIDENCE_DIR/metrics-health.json"'
        ),
        "runtime-dependencies": (
            '"$OBELISK_PYTHON" scripts/check_runtime_dependencies.py '
            '--status-url "${UAM_SYSTEM_STATUS_URL:-http://127.0.0.1:6798/v1/system/status}" '
            '--report "$OBELISK_EVIDENCE_DIR/runtime-dependencies-health.json"'
        ),
        "conversation-retention": (
            '"$OBELISK_PYTHON" scripts/purge_expired_conversations.py '
            '--base-url "${UAM_INTERNAL_BASE_URL:-http://127.0.0.1:6798}" '
            '--tenant-id "$UAM_SERVER_ID" --workspace-id "$UAM_PROJECT_ID" '
            '> "$OBELISK_EVIDENCE_DIR/conversation-retention.json"'
        ),
        "semantic-recovery": (
            'latest_backup="$(find "$OBELISK_BACKUP_DIR" -maxdepth 1 -type f '
            '-name "*.dump.enc" -print | sort | tail -n 1)"\n'
            '[[ -n "$latest_backup" ]]\n'
            'stamp="$(date -u +%Y%m%dT%H%M%SZ)"\n'
            'runtime_env="${OBELISK_RUNTIME_ENV_FILE:-$PWD/.env}"\n'
            '"$OBELISK_PYTHON" scripts/isolated_recovery_drill.py "$latest_backup" '
            '--runtime-env-file "$runtime_env" '
            '--report "$OBELISK_EVIDENCE_DIR/isolated-semantic-recovery-${stamp}.json"'
        ),
    }
    return "\n".join(
        (
            "#!/bin/zsh",
            "set -euo pipefail",
            "set -a",
            f'. "{env_file}"',
            "set +a",
            f'cd "{workspace}"',
            'export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"',
            'mkdir -p "$OBELISK_EVIDENCE_DIR" "$OBELISK_BACKUP_DIR" "$OBELISK_AUDIT_DIR"',
            commands[task],
            "",
        )
    )


def install(*, workspace: Path, env_file: Path, launch_agents: Path) -> list[Path]:
    """Write wrappers and plists; return all generated plist paths."""
    install_dir = launch_agents / "obelisk-memory"
    install_dir.mkdir(parents=True, exist_ok=True)
    schedule = {
        "conversation-retention": (2, 47, None),
        "backup": (3, 23, None),
        "maintenance": (4, 7, None),
        "audit-retention": (4, 37, None),
        "semantic-recovery": (5, 13, 0),
        "metrics": (9, 17, None),
        "runtime-dependencies": (9, 27, None),
    }
    result: list[Path] = []
    for task, (hour, minute, weekday) in schedule.items():
        wrapper = install_dir / f"{task}.zsh"
        wrapper.write_text(_wrapper(workspace, env_file, task), encoding="utf-8")
        wrapper.chmod(0o700)
        plist = launch_agents / f"{LABEL_PREFIX}.{task}.plist"
        with plist.open("wb") as handle:
            plistlib.dump(
                _job(f"{LABEL_PREFIX}.{task}", wrapper, hour, minute, weekday), handle
            )
        plist.chmod(0o600)
        result.append(plist)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--launch-agents", type=Path, default=Path.home() / "Library/LaunchAgents")
    args = parser.parse_args()
    if not args.env_file.is_file():
        parser.error(f"env file does not exist: {args.env_file}")
    plists = install(
        workspace=args.workspace.resolve(),
        env_file=args.env_file.resolve(),
        launch_agents=args.launch_agents,
    )
    for path in plists:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
