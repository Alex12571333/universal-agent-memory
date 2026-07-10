"""Restore a PostgreSQL custom-format backup for the memory server."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from memory_plane.config.secrets import read_secret_env


def main() -> int:
    """Run pg_restore without ownership/ACL assumptions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", help="Path to a pg_dump custom-format file")
    parser.add_argument(
        "--database-url",
        default=read_secret_env(
            "UAM_RESTORE_DATABASE_URL",
            "UAM_ADMIN_DATABASE_URL",
            "UAM_DATABASE_URL",
        ),
        help="PostgreSQL connection URL; defaults to UAM_RESTORE_DATABASE_URL",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Drop database objects before restoring; destructive on the target DB",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("database URL is required")
    backup = Path(args.backup)
    if not backup.exists():
        parser.error(f"backup file does not exist: {backup}")

    command = [
        "pg_restore",
        "--no-owner",
        "--no-acl",
        f"--dbname={args.database_url}",
    ]
    if args.clean:
        command.extend(["--clean", "--if-exists"])
    command.append(str(backup))
    subprocess.run(command, check=True)
    print(backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
