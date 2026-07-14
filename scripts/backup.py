"""Create a PostgreSQL custom-format backup for the memory server."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from memory_plane.config.database import read_database_dsn
from memory_plane.config.postgres_process import (
    password_free_postgres_dsn,
    postgres_process_connection,
)


def _default_database_dsn() -> str | None:
    return (
        read_database_dsn(
            "UAM_BACKUP_DATABASE_URL",
            component_prefix="UAM_BACKUP_DATABASE",
        )
        or read_database_dsn(
            "UAM_ADMIN_DATABASE_URL",
            component_prefix="UAM_ADMIN_DATABASE",
        )
        or read_database_dsn()
    )


def main() -> int:
    """Run pg_dump with safe defaults for Docker/self-hosted deployments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="Path to write the .dump file")
    parser.add_argument(
        "--database-url",
        default=_default_database_dsn(),
        help="PostgreSQL connection URL; defaults to UAM_BACKUP_DATABASE_URL",
    )
    parser.add_argument(
        "--docker-service",
        default=os.getenv("UAM_BACKUP_DOCKER_SERVICE", "postgres"),
        help="Compose PostgreSQL service used only when host pg_dump is unavailable",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("database URL is required")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("pg_dump"):
        with postgres_process_connection(args.database_url) as connection:
            subprocess.run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    f"--file={output}",
                    connection.dsn,
                ],
                check=True,
                env=connection.environment,
            )
    else:
        _docker_pg_dump(output, args.database_url, args.docker_service)
    print(output)
    return 0


def _docker_pg_dump(output: Path, dsn: str, service: str) -> None:
    """Use the appliance's postgres container when macOS lacks pg_dump."""
    if not shutil.which("docker"):
        raise FileNotFoundError("pg_dump is unavailable and Docker is not installed")
    if not service or any(char.isspace() for char in service):
        raise ValueError("docker backup service must be a non-empty Compose service name")
    remote = f"/tmp/obelisk-backup-{uuid4().hex}.dump"
    container_dsn, password = password_free_postgres_dsn(
        dsn,
        host="127.0.0.1",
        port=5432,
    )
    try:
        subprocess.run(
            [
                "docker", "compose", "exec", "-T", service, "sh", "-c",
                "IFS= read -r supplied; "
                "PGPASSWORD=${supplied:-${POSTGRES_PASSWORD:-}}; export PGPASSWORD; "
                'exec "$@"',
                "obelisk-pg",
                "pg_dump", "--format=custom", "--no-owner", "--no-acl",
                f"--file={remote}", container_dsn,
            ],
            check=True,
            input=f"{password or ''}\n",
            text=True,
        )
        subprocess.run(
            ["docker", "compose", "cp", f"{service}:{remote}", str(output)],
            check=True,
        )
    finally:
        subprocess.run(
            ["docker", "compose", "exec", "-T", service, "rm", "-f", remote],
            check=False,
        )


if __name__ == "__main__":
    raise SystemExit(main())
