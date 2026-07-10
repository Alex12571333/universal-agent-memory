"""Create a PostgreSQL custom-format backup for the memory server."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from memory_plane.config.database import read_database_dsn


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
    args = parser.parse_args()
    if not args.database_url:
        parser.error("database URL is required")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        f"--file={output}",
        args.database_url,
    ]
    subprocess.run(command, check=True)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
