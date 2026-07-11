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


def _job(label: str, script: Path, hour: int, minute: int) -> dict[object, object]:
    return {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", str(script)],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
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
            '--report "$OBELISK_EVIDENCE_DIR/latest-backup-report.json"'
        ),
        "maintenance": (
            '"$OBELISK_PYTHON" scripts/maintenance_retention.py '
            '--database-url "$UAM_MAINTENANCE_DATABASE_URL" --apply '
            '--report "$OBELISK_EVIDENCE_DIR/maintenance-retention.json"'
        ),
        "metrics": (
            '"$OBELISK_PYTHON" scripts/check_metrics_health.py '
            '--metrics-url "${UAM_METRICS_URL:-http://127.0.0.1:6798/metrics}" '
            '--report "$OBELISK_EVIDENCE_DIR/metrics-health.json"'
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
    schedule = {"backup": (3, 23), "maintenance": (3, 37), "metrics": (9, 17)}
    result: list[Path] = []
    for task, (hour, minute) in schedule.items():
        wrapper = install_dir / f"{task}.zsh"
        wrapper.write_text(_wrapper(workspace, env_file, task), encoding="utf-8")
        wrapper.chmod(0o700)
        plist = launch_agents / f"{LABEL_PREFIX}.{task}.plist"
        with plist.open("wb") as handle:
            plistlib.dump(_job(f"{LABEL_PREFIX}.{task}", wrapper, hour, minute), handle)
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
