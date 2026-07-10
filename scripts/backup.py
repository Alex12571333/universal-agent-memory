"""Create a PostgreSQL custom-format backup for the memory server."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from memory_plane.config.secrets import read_secret_env


def main() -> int:
    """Run pg_dump with safe defaults for Docker/self-hosted deployments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="Path to write the .dump file")
    parser.add_argument(
        "--database-url",
        default=read_secret_env(
            "UAM_BACKUP_DATABASE_URL",
            "UAM_ADMIN_DATABASE_URL",
            "UAM_DATABASE_URL",
        ),
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
