"""Restore a backup into a temporary PostgreSQL container and verify schema health."""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

from backup_encryption import BackupEncryptionError, decrypt_file, parse_key

from memory_plane.config.secrets import read_secret_env

REQUIRED_TABLES = (
    "schema_migrations",
    "memory_items",
    "memory_provenance",
    "outbox_events",
    "conversation_turns",
    "memory_proposals",
    "audit_events",
    "api_key_registry",
)


def main() -> int:
    """Run a non-destructive restore drill against an isolated Docker volume."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", help="Path to a .dump or AES-256-GCM encrypted .dump.enc file")
    parser.add_argument(
        "--encryption-key",
        default=read_secret_env("UAM_BACKUP_ENCRYPTION_KEY"),
        help="Required for .enc artifacts; defaults to UAM_BACKUP_ENCRYPTION_KEY[_FILE]",
    )
    parser.add_argument(
        "--image",
        default="postgres:17-alpine",
        help="PostgreSQL image used for the temporary restore target",
    )
    parser.add_argument(
        "--name-prefix",
        default="obelisk-restore-drill",
        help="Prefix for temporary Docker container and volume names",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=60,
        help="How long to wait for temporary PostgreSQL readiness",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the temporary container and volume for manual inspection",
    )
    args = parser.parse_args()

    backup = Path(args.backup)
    if not backup.exists():
        parser.error(f"backup file does not exist: {backup}")

    suffix = secrets.token_hex(4)
    container = f"{args.name_prefix}-{suffix}"
    volume = f"{container}-data"
    db = "memory"
    user = "memory_admin"
    password = f"drill-{secrets.token_hex(12)}"
    dsn = f"postgresql://{user}:{password}@localhost:5432/{db}"
    remote_backup = "/tmp/obelisk-memory.dump"
    decrypted_backup: Path | None = None

    if backup.suffix == ".enc":
        if not args.encryption_key:
            parser.error("encrypted backup requires UAM_BACKUP_ENCRYPTION_KEY or --encryption-key")
        try:
            key = parse_key(args.encryption_key)
        except BackupEncryptionError as exc:
            parser.error(str(exc))
        descriptor, name = tempfile.mkstemp(prefix="obelisk-restore-", suffix=".dump")
        os.close(descriptor)
        decrypted_backup = Path(name)
        decrypted_backup.chmod(0o600)
        try:
            decrypt_file(backup, decrypted_backup, key)
        except BackupEncryptionError as exc:
            decrypted_backup.unlink(missing_ok=True)
            raise RuntimeError(f"unable to decrypt backup: {exc}") from exc
        backup = decrypted_backup

    try:
        _run(["docker", "volume", "create", volume])
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-e",
                f"POSTGRES_DB={db}",
                "-e",
                f"POSTGRES_USER={user}",
                "-e",
                f"POSTGRES_PASSWORD={password}",
                "-v",
                f"{volume}:/var/lib/postgresql/data",
                args.image,
            ]
        )
        _wait_for_postgres(container, user, db, args.timeout_seconds)
        _run(["docker", "cp", str(backup), f"{container}:{remote_backup}"])
        _run(
            [
                "docker",
                "exec",
                container,
                "pg_restore",
                "--no-owner",
                "--no-acl",
                f"--dbname={dsn}",
                remote_backup,
            ]
        )
        _verify_schema(container, dsn)
        print(f"restore_drill=PASS container={container} volume={volume}")
        return 0
    finally:
        if decrypted_backup is not None:
            decrypted_backup.unlink(missing_ok=True)
        if not args.keep:
            _run(["docker", "rm", "-f", container], check=False)
            _run(["docker", "volume", "rm", "-f", volume], check=False)


def _wait_for_postgres(
    container: str,
    user: str,
    db: str,
    timeout_seconds: int,
) -> None:
    """Wait until the temporary PostgreSQL accepts local connections."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = _run(
            [
                "docker",
                "exec",
                container,
                "pg_isready",
                "-U",
                user,
                "-d",
                db,
            ],
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"temporary PostgreSQL did not become ready: {container}")


def _verify_schema(container: str, dsn: str) -> None:
    """Check that restored schema contains required production tables."""
    required_values = ",".join(f"('{table}')" for table in REQUIRED_TABLES)
    sql = f"""
    with required(name) as (values {required_values})
    select name
    from required
    where to_regclass(name) is null
    order by name;
    select count(*) from schema_migrations;
    select count(*) from memory_items;
    select count(*) from audit_events;
    select count(*) from api_key_registry;
    """
    result = _run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            f"--dbname={dsn}",
            "--command",
            sql,
        ],
        capture_output=True,
    )
    output = result.stdout.strip()
    first_section = output.splitlines()[0 : len(REQUIRED_TABLES)]
    missing = [line for line in first_section if line in REQUIRED_TABLES]
    if missing:
        raise RuntimeError(f"restore drill missing tables: {', '.join(missing)}")
    print("restore_drill_verified_tables=" + ",".join(REQUIRED_TABLES))


def _run(
    command: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command with consistent text output settings."""
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture_output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
