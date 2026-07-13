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
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

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
        default=None,
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
    try:
        backup_encryption_key = _backup_encryption_key(args.runtime_env_file)
        backup_snapshot_report = _backup_snapshot_report(args.backup)
        source_database_url = (
            None
            if backup_snapshot_report is not None
            else _source_database_url(args.source_database_url, args.runtime_env_file)
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    suffix = secrets.token_hex(4)
    postgres_container: str | None = None
    qdrant_container: str | None = None
    with tempfile.TemporaryDirectory(prefix="obelisk-isolated-recovery-") as temporary:
        work_dir = Path(temporary)
        restore_report = work_dir / "restore-drill.json"
        probe_report = work_dir / "restored-reindex-probe.json"
        stage = "restore"
        try:
            restore = _run_restore_drill(
                args,
                suffix,
                restore_report,
                backup_encryption_key=backup_encryption_key,
                source_database_url=source_database_url,
                expected_row_counts_report=backup_snapshot_report,
            )
            postgres_container = _restore_container(restore.stdout)
            password = _postgres_password(postgres_container)
            stage = "qdrant"
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
            stage = "semantic-probe"
            collection = f"recovery_probe_{suffix}"
            docker_runtime_env = _materialize_docker_env_file(
                args.runtime_env_file,
                work_dir / "runtime-for-docker.env",
            )
            _run_probe(
                args,
                postgres_container=postgres_container,
                postgres_password=password,
                collection=collection,
                output=probe_report,
                work_dir=work_dir,
                runtime_env_file=docker_runtime_env,
            )
            persisted_restore, persisted_probe = _persist_evidence(
                restore_report, probe_report, args.report
            )
            stage = "bind-evidence"
            _bind_evidence(persisted_restore, persisted_probe, args.report)
            print(args.report.read_text(encoding="utf-8").strip())
            return 0
        except Exception as exc:  # noqa: BLE001 - emit non-secret failure evidence.
            _write_failure_report(args.report, type(exc).__name__, stage)
            print(
                f"isolated_recovery_drill=FAIL stage={stage} error_type={type(exc).__name__}",
                file=sys.stderr,
            )
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
    args: argparse.Namespace,
    suffix: str,
    report: Path,
    *,
    backup_encryption_key: str,
    source_database_url: str | None,
    expected_row_counts_report: Path | None,
) -> subprocess.CompletedProcess[str]:
    """Run restore with recovery secrets in child environment, never argv/evidence."""
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
    if args.source_docker_service:
        command.extend(["--source-docker-service", args.source_docker_service])
    environment = os.environ.copy()
    environment["UAM_BACKUP_ENCRYPTION_KEY"] = backup_encryption_key
    if source_database_url:
        environment["UAM_BACKUP_DATABASE_URL"] = source_database_url
    if expected_row_counts_report:
        command.extend(["--expected-row-counts-report", str(expected_row_counts_report)])
    return _run(command, capture_output=True, env=environment)


def _backup_snapshot_report(backup: Path) -> Path | None:
    """Return the adjacent report only when it carries a usable count snapshot."""
    report = backup.with_suffix("").with_suffix(".restore.json")
    if not report.is_file():
        return None
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return report if isinstance(payload.get("source_row_counts"), dict) else None


def _backup_encryption_key(runtime_env_file: Path) -> str:
    """Load the recovery key from process env or the selected runtime env file.

    The launchd job sources its private ops file, while a manual drill commonly
    supplies only ``--runtime-env-file``.  Both paths must work.  The key is
    returned only to populate the nested restore process environment; it is not
    placed in command-line arguments, reports, or exception details.
    """
    direct = os.getenv("UAM_BACKUP_ENCRYPTION_KEY", "").strip()
    if direct:
        return direct
    process_file = os.getenv("UAM_BACKUP_ENCRYPTION_KEY_FILE", "").strip()
    if process_file:
        return _read_secret_file(Path(process_file))
    values = _parse_env_file(runtime_env_file)
    configured = values.get("UAM_BACKUP_ENCRYPTION_KEY", "").strip()
    if configured:
        return configured
    configured_file = values.get("UAM_BACKUP_ENCRYPTION_KEY_FILE", "").strip()
    if configured_file:
        path = Path(configured_file)
        if not path.is_absolute():
            path = runtime_env_file.parent / path
        return _read_secret_file(path)
    raise ValueError(
        "encrypted recovery drill requires UAM_BACKUP_ENCRYPTION_KEY or "
        "UAM_BACKUP_ENCRYPTION_KEY_FILE"
    )


def _source_database_url(explicit: str | None, runtime_env_file: Path) -> str:
    """Resolve the parity source DSN without putting it in child argv."""
    if explicit and explicit.strip():
        return explicit.strip()
    for name in ("UAM_BACKUP_DATABASE_URL", "UAM_ADMIN_DATABASE_URL", "UAM_DATABASE_URL"):
        configured = _environment_secret(name)
        if configured:
            return configured
    values = _parse_env_file(runtime_env_file)
    for name in ("UAM_BACKUP_DATABASE_URL", "UAM_ADMIN_DATABASE_URL", "UAM_DATABASE_URL"):
        configured = _file_secret(values, runtime_env_file, name)
        if configured:
            return configured
    raise ValueError(
        "recovery drill requires a source PostgreSQL URL for row-parity verification"
    )


def _environment_secret(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    if value:
        return value
    file_name = os.getenv(f"{name}_FILE", "").strip()
    return _read_secret_file(Path(file_name)) if file_name else None


def _file_secret(values: dict[str, str], runtime_env_file: Path, name: str) -> str | None:
    value = values.get(name, "").strip()
    if value:
        return value
    file_name = values.get(f"{name}_FILE", "").strip()
    if not file_name:
        return None
    path = Path(file_name)
    if not path.is_absolute():
        path = runtime_env_file.parent / path
    return _read_secret_file(path)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Read the conservative KEY=VALUE syntax supported by deployment env files."""
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.replace("_", "").isalnum() or key[:1].isdigit():
            raise ValueError(f"{path}:{line_number}: invalid env key")
        values[key] = _strip_env_quotes(value.strip())
    return values


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_secret_file(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("backup encryption key file is empty")
    return value


def _materialize_docker_env_file(source: Path, target: Path) -> Path:
    """Translate Compose-style dotenv quoting to Docker ``--env-file`` syntax.

    Docker's raw ``--env-file`` parser retains single quotes, whereas Compose
    strips them.  That distinction breaks JSON-valued settings such as the
    Qwen extra-body profile during an isolated recovery probe.  The translated
    file is mode-0600 inside the temporary recovery directory and is removed
    with that directory after the drill.
    """
    values = _parse_env_file(source)
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"{source}: {key} contains a newline")
    target.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    target.chmod(0o600)
    return target


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
    runtime_env_file: Path | None = None,
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
        str((runtime_env_file or args.runtime_env_file).resolve()),
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


def _write_failure_report(path: Path, error_type: str, stage: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "format": "obelisk-isolated-semantic-recovery-drill-v1",
                "ok": False,
                "error_type": error_type,
                "stage": stage,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _run(
    command: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture_output,
        env=env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
