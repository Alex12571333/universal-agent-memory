"""Restore a backup into a temporary PostgreSQL container and verify schema health."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from backup_encryption import BackupEncryptionError, decrypt_file, parse_key

from memory_plane.config.database import read_database_dsn
from memory_plane.config.postgres_process import (
    password_free_postgres_dsn,
    postgres_process_connection,
)
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
    "worker_heartbeats",
)

RLS_TABLES = (
    "workspaces",
    "agents",
    "threads",
    "memory_items",
    "memory_provenance",
    "memory_edges",
    "observations",
    "observation_evidence",
    "idempotency_keys",
    "outbox_events",
    "checkpoints",
    "processed_events",
    "conflict_reviews",
    "conversation_turns",
    "conversation_messages",
    "conversation_idempotency_keys",
    "memory_proposals",
    "memory_proposal_idempotency_keys",
    "audit_events",
    "api_key_registry",
    "worker_heartbeats",
)

REPORT_FORMAT = "obelisk-restore-drill-v1"


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
    parser.add_argument(
        "--source-database-url",
        default=(
            read_database_dsn(
                "UAM_BACKUP_DATABASE_URL",
                component_prefix="UAM_BACKUP_DATABASE",
            )
            or read_database_dsn()
        ),
        help="Optional source PostgreSQL URL used to verify restored row counts",
    )
    parser.add_argument(
        "--source-docker-service",
        default=os.getenv("UAM_BACKUP_DOCKER_SERVICE", "postgres"),
        help="Compose PostgreSQL service used for source parity when host psql is unavailable",
    )
    parser.add_argument(
        "--expected-row-counts-report",
        type=Path,
        help=(
            "Prior restore-drill report for this exact backup; verifies restored "
            "rows against its non-secret source_row_counts snapshot"
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional JSON report path for the successful isolated restore drill",
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
    # Every restore command runs inside the temporary PostgreSQL container.
    # Use the same Unix socket that readiness checks, avoiding a race where the
    # socket is ready before the TCP listener accepts localhost connections.
    dsn = f"postgresql:///{db}?user={user}"
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

    env_descriptor, env_name = tempfile.mkstemp(prefix="obelisk-restore-docker-")
    os.fchmod(env_descriptor, 0o600)
    with os.fdopen(env_descriptor, "w", encoding="utf-8") as env_handle:
        env_handle.write(f"POSTGRES_DB={db}\n")
        env_handle.write(f"POSTGRES_USER={user}\n")
        env_handle.write(f"POSTGRES_PASSWORD={password}\n")
        env_handle.write(f"PGPASSWORD={password}\n")
    container_env_file = Path(env_name)

    try:
        _run(["docker", "volume", "create", volume])
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--env-file",
                str(container_env_file),
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
        _verify_rls(container, dsn)
        checks = [
            {"name": "required-schema", "ok": True},
            {"name": "forced-tenant-rls", "ok": True},
        ]
        source_row_counts: dict[str, int] | None = None
        if args.expected_row_counts_report:
            source_row_counts = _expected_row_counts(args.expected_row_counts_report)
            _verify_expected_row_counts(container, dsn, source_row_counts)
            checks.append({"name": "source-row-parity", "ok": True})
        elif args.source_database_url:
            source_row_counts = _verify_row_parity(
                args.source_database_url,
                container,
                dsn,
                source_docker_service=args.source_docker_service,
            )
            checks.append({"name": "source-row-parity", "ok": True})
        if args.report:
            _write_report(args.report, checks, source_row_counts=source_row_counts)
        print(f"restore_drill=PASS container={container} volume={volume}")
        return 0
    finally:
        if decrypted_backup is not None:
            decrypted_backup.unlink(missing_ok=True)
        container_env_file.unlink(missing_ok=True)
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
                "psql",
                "--no-psqlrc",
                "--username",
                user,
                "--dbname",
                db,
                "--tuples-only",
                "--no-align",
                "--command",
                "select 1",
            ],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0 and result.stdout.strip() == "1":
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
    select count(*) from worker_heartbeats;
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


def _verify_row_parity(
    source_dsn: str,
    container: str,
    restored_dsn: str,
    *,
    source_docker_service: str,
) -> dict[str, int]:
    """Reject a restore that lost rows from critical durable tables."""
    tables = tuple(table for table in REQUIRED_TABLES if table != "schema_migrations")
    sql = "\n".join(
        f"select '{table}', count(*) from {table};" for table in tables
    )
    source = _query_source_counts(source_dsn, sql, source_docker_service)
    restored = _run(
        [
            "docker", "exec", container, "psql", "--tuples-only", "--no-align",
            f"--dbname={restored_dsn}", "--command", sql,
        ],
        capture_output=True,
    )
    source_counts = _parse_counts(source.stdout)
    restored_counts = _parse_counts(restored.stdout)
    if source_counts != restored_counts:
        raise RuntimeError(
            f"restore drill row parity failed: source={source_counts} restored={restored_counts}"
        )
    print("restore_drill_row_parity=PASS")
    return source_counts


def _verify_expected_row_counts(
    container: str, restored_dsn: str, expected: dict[str, int]
) -> None:
    """Validate an old backup against its snapshot instead of a changing live DB."""
    tables = tuple(table for table in REQUIRED_TABLES if table != "schema_migrations")
    sql = "\n".join(f"select '{table}', count(*) from {table};" for table in tables)
    restored = _run(
        [
            "docker", "exec", container, "psql", "--tuples-only", "--no-align",
            f"--dbname={restored_dsn}", "--command", sql,
        ],
        capture_output=True,
    )
    restored_counts = _parse_counts(restored.stdout)
    if expected != restored_counts:
        raise RuntimeError("restore drill row parity failed against backup snapshot")
    print("restore_drill_row_parity_snapshot=PASS")


def _expected_row_counts(report: Path) -> dict[str, int]:
    """Read only the numeric source snapshot from a prior successful drill."""
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("expected row-count report is unreadable") from exc
    counts = payload.get("source_row_counts")
    tables = tuple(table for table in REQUIRED_TABLES if table != "schema_migrations")
    if (
        payload.get("format") != REPORT_FORMAT
        or payload.get("ok") is not True
        or not isinstance(counts, dict)
        or set(counts) != set(tables)
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in counts.values()
        )
    ):
        raise ValueError("expected row-count report has no valid backup snapshot")
    return {table: int(counts[table]) for table in tables}


def _query_source_counts(
    source_dsn: str,
    sql: str,
    docker_service: str,
) -> subprocess.CompletedProcess[str]:
    """Query source rows with host psql or the local Compose PostgreSQL fallback."""
    psql_args = ["psql", "--tuples-only", "--no-align"]
    if shutil.which("psql"):
        with postgres_process_connection(source_dsn) as connection:
            return _run(
                [*psql_args, f"--dbname={connection.dsn}", "--command", sql],
                capture_output=True,
                env=connection.environment,
            )
    if not docker_service or any(character.isspace() for character in docker_service):
        raise ValueError("source Docker service must be a non-empty Compose service name")
    sanitized_dsn, password = password_free_postgres_dsn(
        _compose_source_dsn(source_dsn)
    )
    return _run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            docker_service,
            "sh",
            "-c",
            "IFS= read -r supplied; "
            "PGPASSWORD=${supplied:-${POSTGRES_PASSWORD:-}}; export PGPASSWORD; "
            'exec "$@"',
            "obelisk-pg",
            *psql_args,
            f"--dbname={sanitized_dsn}",
            "--command",
            sql,
        ],
        capture_output=True,
        input_text=f"{password or ''}\n",
    )


def _compose_source_dsn(source_dsn: str) -> str:
    """Map the exposed local Compose PostgreSQL port to the service-local port."""
    for host in ("127.0.0.1", "localhost"):
        marker = f"@{host}:6548/"
        if marker in source_dsn:
            return source_dsn.replace(marker, "@127.0.0.1:5432/", 1)
    raise ValueError(
        "host psql is unavailable; Docker fallback supports a local Compose "
        "source URL at 127.0.0.1:6548 or localhost:6548"
    )


def _verify_rls(container: str, dsn: str) -> None:
    """Reject a restore that weakens tenant isolation at the database layer."""
    required_values = ",".join(f"('{table}')" for table in RLS_TABLES)
    sql = f"""
    with required(name) as (values {required_values}),
    missing_table_protection as (
      select required.name
      from required
      left join pg_class relation
        on relation.relname = required.name
       and relation.relnamespace = 'public'::regnamespace
      where relation.oid is null
         or coalesce(relation.relrowsecurity, false) is false
         or coalesce(relation.relforcerowsecurity, false) is false
    ),
    missing_policy as (
      select required.name
      from required
      where not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = required.name
          and policyname = 'tenant_isolation'
      )
    )
    select name from missing_table_protection
    union
    select name from missing_policy
    order by name;
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
    missing = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if missing:
        raise RuntimeError(f"restore drill RLS verification failed: {', '.join(missing)}")
    print("restore_drill_rls=PASS")


def _parse_counts(output: str) -> dict[str, int]:
    return {
        table: int(count)
        for line in output.splitlines()
        if (parts := line.strip().split("|", 1)) and len(parts) == 2
        for table, count in [parts]
    }


def _write_report(
    path: Path,
    checks: list[dict[str, object]],
    *,
    source_row_counts: dict[str, int] | None = None,
) -> None:
    """Persist non-secret success evidence that can be bound into a backup bundle."""
    payload = {
        "format": REPORT_FORMAT,
        "ok": all(check.get("ok") is True for check in checks),
        "completed_at": datetime.now(UTC).isoformat(),
        "checks": checks,
    }
    if source_row_counts is not None:
        payload["source_row_counts"] = source_row_counts
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _run(
    command: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command with consistent text output settings."""
    options: dict[str, object] = {
        "check": check,
        "text": True,
        "capture_output": capture_output,
    }
    if env is not None:
        options["env"] = env
    if input_text is not None:
        options["input"] = input_text
    return subprocess.run(command, **options)


if __name__ == "__main__":
    raise SystemExit(main())
