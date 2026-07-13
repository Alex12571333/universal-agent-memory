"""Prove that an encrypted backup can rebuild semantic recall in isolation.

The drill deliberately never mounts or queries the live Qdrant collection.  It
restores PostgreSQL through ``restore_drill.py``, starts an empty Qdrant in the
same temporary network namespace, reindexes the restored ledger using the
Obelisk image, and records the normal recovery-evidence report.  Unless
``--keep`` is selected, every temporary container and volume is removed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from memory_plane.config.database import read_database_dsn

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER_IMAGE = "universal-agent-memory-memory-server:latest"
DEFAULT_QDRANT_IMAGE = "qdrant/qdrant:v1.12.6"
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000002"
RESTORE_CONTAINER_PATTERN = re.compile(r"restore_drill=PASS container=([^\s]+)")


def main() -> int:
    """Run the complete, non-destructive restored-ledger semantic recovery drill."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path, help="Encrypted or plaintext PostgreSQL dump")
    parser.add_argument("--report", type=Path, required=True, help="Final recovery-evidence JSON")
    parser.add_argument("--tenant-id", default=os.getenv("UAM_SERVER_ID", DEFAULT_TENANT_ID))
    parser.add_argument(
        "--workspace-id", default=os.getenv("UAM_PROJECT_ID", DEFAULT_WORKSPACE_ID)
    )
    parser.add_argument(
        "--runtime-env-file",
        type=Path,
        default=ROOT / ".env",
        help="Environment file with pgcrypto and embedding settings for the probe",
    )
    parser.add_argument("--server-image", default=DEFAULT_SERVER_IMAGE)
    parser.add_argument("--qdrant-image", default=DEFAULT_QDRANT_IMAGE)
    parser.add_argument("--name-prefix", default="obelisk-isolated-recovery")
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument(
        "--source-database-url",
        default=(
            os.getenv("UAM_BACKUP_DATABASE_URL")
            or read_database_dsn(
                "UAM_BACKUP_DATABASE_URL", component_prefix="UAM_BACKUP_DATABASE"
            )
            or read_database_dsn("UAM_ADMIN_DATABASE_URL", component_prefix="UAM_ADMIN_DATABASE")
            or read_database_dsn()
        ),
        help="Source PostgreSQL URL used by restore drill for row-parity verification",
    )
    parser.add_argument(
        "--source-docker-service",
        default=os.getenv("UAM_BACKUP_DOCKER_SERVICE", "postgres"),
        help="Compose PostgreSQL service used when host psql is unavailable",
    )
    parser.add_argument(
        "--keep", action="store_true", help="Keep temporary containers for forensics"
    )
    args = parser.parse_args()

    if not args.backup.is_file():
        parser.error(f"backup file does not exist: {args.backup}")
    if not args.runtime_env_file.is_file():
        parser.error(f"runtime env file does not exist: {args.runtime_env_file}")

    suffix = secrets.token_hex(4)
    postgres_container: str | None = None
    qdrant_container: str | None = None
    with tempfile.TemporaryDirectory(prefix="obelisk-isolated-recovery-") as temporary:
        work_dir = Path(temporary)
        restore_report = work_dir / "restore-drill.json"
        probe_report = work_dir / "restored-reindex-probe.json"
        try:
            restore = _run_restore_drill(args, suffix, restore_report)
            postgres_container = _restore_container(restore.stdout)
            password = _postgres_password(postgres_container)
            qdrant_container = f"{args.name_prefix}-qdrant-{suffix}"
            _run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    qdrant_container,
                    "--network",
                    f"container:{postgres_container}",
                    args.qdrant_image,
                ]
            )
            _wait_for_qdrant(postgres_container, args.server_image, args.timeout_seconds)
            collection = f"recovery_probe_{suffix}"
            _run_probe(
                args,
                postgres_container=postgres_container,
                postgres_password=password,
                collection=collection,
                output=probe_report,
                work_dir=work_dir,
            )
            persisted_restore, persisted_probe = _persist_evidence(
                restore_report, probe_report, args.report
            )
            _bind_evidence(persisted_restore, persisted_probe, args.report)
            print(args.report.read_text(encoding="utf-8").strip())
            return 0
        except Exception as exc:  # noqa: BLE001 - emit non-secret failure evidence.
            _write_failure_report(args.report, type(exc).__name__)
            print(f"isolated_recovery_drill=FAIL error_type={type(exc).__name__}", file=sys.stderr)
            return 1
        finally:
            if not args.keep:
                if qdrant_container:
                    _run(
                        ["docker", "rm", "-f", qdrant_container],
                        check=False,
                        capture_output=True,
                    )
                if postgres_container:
                    _run(
                        ["docker", "rm", "-f", postgres_container],
                        check=False,
                        capture_output=True,
                    )
                    _run(
                        ["docker", "volume", "rm", "-f", f"{postgres_container}-data"],
                        check=False,
                        capture_output=True,
                    )


def _run_restore_drill(
    args: argparse.Namespace, suffix: str, report: Path
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "restore_drill.py"),
        str(args.backup),
        "--name-prefix",
        args.name_prefix,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--keep",
        "--report",
        str(report),
    ]
    if args.source_database_url:
        command.extend(["--source-database-url", args.source_database_url])
    if args.source_docker_service:
        command.extend(["--source-docker-service", args.source_docker_service])
    return _run(command, capture_output=True)


def _restore_container(output: str) -> str:
    """Read the generated container name without relying on predictable IDs."""
    match = RESTORE_CONTAINER_PATTERN.search(output)
    if not match:
        raise RuntimeError("restore drill did not report its temporary container")
    return match.group(1)


def _postgres_password(container: str) -> str:
    """Read the ephemeral target password locally without persisting it in evidence."""
    result = _run(
        ["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", container],
        capture_output=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            password = line.removeprefix("POSTGRES_PASSWORD=")
            if password:
                return password
    raise RuntimeError("temporary PostgreSQL password was not available")


def _wait_for_qdrant(
    network_container: str, server_image: str, timeout_seconds: int
) -> None:
    """Wait for Qdrant's readiness endpoint inside the isolated namespace."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = _run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                f"container:{network_container}",
                server_image,
                "python",
                "-c",
                (
                    "import urllib.request; "
                    "urllib.request.urlopen('http://127.0.0.1:6333/readyz', timeout=2)"
                ),
            ],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            time.sleep(1)
            return
        time.sleep(1)
    raise RuntimeError("temporary Qdrant did not become ready")


def _run_probe(
    args: argparse.Namespace,
    *,
    postgres_container: str,
    postgres_password: str,
    collection: str,
    output: Path,
    work_dir: Path,
) -> None:
    dsn = "postgresql://memory_admin:{}@127.0.0.1:5432/memory".format(
        quote(postgres_password, safe="")
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        f"container:{postgres_container}",
        "--env-file",
        str(args.runtime_env_file.resolve()),
        "-e",
        f"UAM_DATABASE_URL={dsn}",
        "-e",
        "UAM_QDRANT_URL=http://127.0.0.1:6333",
        "-v",
        f"{work_dir}:/evidence",
        args.server_image,
        "python",
        "scripts/restore_reindex_probe.py",
        "--tenant-id",
        args.tenant_id,
        "--workspace-id",
        args.workspace_id,
        "--qdrant-url",
        "http://127.0.0.1:6333",
        "--collection",
        collection,
        "--report",
        f"/evidence/{output.name}",
    ]
    _run(command)


def _bind_evidence(restore_report: Path, probe_report: Path, target: Path) -> None:
    _run(
        [
            sys.executable,
            str(ROOT / "scripts" / "restore_recovery_evidence.py"),
            "--restore-report",
            str(restore_report),
            "--reindex-report",
            str(probe_report),
            "--semantic-report",
            str(probe_report),
            "--report",
            str(target),
        ]
    )


def _persist_evidence(restore_report: Path, probe_report: Path, target: Path) -> tuple[Path, Path]:
    """Keep proof inputs after temporary recovery containers are removed."""
    target.parent.mkdir(parents=True, exist_ok=True)
    persisted_restore = target.with_name(f"{target.stem}.restore-drill.json")
    persisted_probe = target.with_name(f"{target.stem}.restored-reindex-probe.json")
    shutil.copyfile(restore_report, persisted_restore)
    shutil.copyfile(probe_report, persisted_probe)
    return persisted_restore, persisted_probe


def _write_failure_report(path: Path, error_type: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "format": "obelisk-isolated-semantic-recovery-drill-v1",
                "ok": False,
                "error_type": error_type,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _run(
    command: list[str], *, check: bool = True, capture_output: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=capture_output)


if __name__ == "__main__":
    raise SystemExit(main())
