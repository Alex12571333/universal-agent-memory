"""Restore a PostgreSQL custom-format backup for the memory server."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

from backup_encryption import BackupEncryptionError, decrypt_file, parse_key

from memory_plane.config.database import read_database_dsn
from memory_plane.config.secrets import read_secret_env


def _default_database_dsn() -> str | None:
    return (
        read_database_dsn(
            "UAM_RESTORE_DATABASE_URL",
            component_prefix="UAM_RESTORE_DATABASE",
        )
        or read_database_dsn(
            "UAM_ADMIN_DATABASE_URL",
            component_prefix="UAM_ADMIN_DATABASE",
        )
        or read_database_dsn()
    )


def main() -> int:
    """Run pg_restore without ownership/ACL assumptions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", help="Path to a .dump or AES-256-GCM encrypted .dump.enc file")
    parser.add_argument(
        "--database-url",
        default=_default_database_dsn(),
        help="PostgreSQL connection URL; defaults to UAM_RESTORE_DATABASE_URL",
    )
    parser.add_argument(
        "--encryption-key",
        default=read_secret_env("UAM_BACKUP_ENCRYPTION_KEY"),
        help="Required for .enc artifacts; defaults to UAM_BACKUP_ENCRYPTION_KEY[_FILE]",
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

    decrypted_backup: Path | None = None
    try:
        if backup.suffix == ".enc":
            if not args.encryption_key:
                parser.error(
                    "encrypted backup requires UAM_BACKUP_ENCRYPTION_KEY or --encryption-key"
                )
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
                raise RuntimeError(f"unable to decrypt backup: {exc}") from exc
            backup = decrypted_backup

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
    finally:
        if decrypted_backup is not None:
            decrypted_backup.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
