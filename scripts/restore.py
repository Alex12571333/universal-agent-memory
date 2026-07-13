"""Restore a PostgreSQL custom-format backup for the memory server."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from backup_encryption import BackupEncryptionError, decrypt_file, parse_key

from memory_plane.config.database import read_database_dsn
from memory_plane.config.secrets import read_secret_env

BUNDLE_FORMAT = "obelisk-backup-bundle-v1"
_SHA256_HEX_LENGTH = 64


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
    parser.add_argument(
        "--bundle-manifest",
        type=Path,
        help="Signed scheduled-backup bundle manifest that authorizes this exact artifact",
    )
    parser.add_argument(
        "--bundle-signing-key",
        default=read_secret_env("UAM_BACKUP_SIGNING_KEY"),
        help="HMAC key for --bundle-manifest; defaults to UAM_BACKUP_SIGNING_KEY[_FILE]",
    )
    parser.add_argument(
        "--require-bundle-signature",
        action="store_true",
        help="Require a valid signed --bundle-manifest before changing the restore database",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("database URL is required")
    backup = Path(args.backup)
    if not backup.exists():
        parser.error(f"backup file does not exist: {backup}")
    if args.require_bundle_signature and args.bundle_manifest is None:
        parser.error("--require-bundle-signature requires --bundle-manifest")
    if args.bundle_manifest is not None:
        try:
            _verify_restore_bundle(
                args.bundle_manifest,
                backup,
                signing_key=args.bundle_signing_key,
                require_signature=args.require_bundle_signature,
            )
        except ValueError as exc:
            parser.error(f"backup bundle verification failed: {exc}")

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


def _verify_restore_bundle(
    manifest_path: Path,
    backup_path: Path,
    *,
    signing_key: str | None,
    require_signature: bool,
) -> None:
    """Authorize one selected backup artifact before a destructive restore.

    The scheduled-bundle manifest may also reference an audit artifact stored
    elsewhere.  Restore only follows the explicitly selected backup path, then
    verifies the manifest signature over the complete manifest; it never opens
    arbitrary paths named by the manifest.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT:
        raise ValueError("unexpected backup bundle manifest format")
    _verify_bundle_signature(manifest, signing_key=signing_key, require_signature=require_signature)

    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("bundle manifest has no files list")
    expected_path = backup_path.resolve(strict=True)
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and _resolved_path(entry["path"]) == expected_path
    ]
    if len(matches) != 1:
        raise ValueError("selected backup is not uniquely authorized by the bundle manifest")
    entry = matches[0]
    expected_digest = entry.get("sha256")
    expected_bytes = entry.get("bytes")
    if (
        not isinstance(expected_digest, str)
        or len(expected_digest) != _SHA256_HEX_LENGTH
        or any(character not in "0123456789abcdefABCDEF" for character in expected_digest)
        or not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes < 0
    ):
        raise ValueError("backup bundle entry has invalid digest or byte count")
    actual_bytes = backup_path.stat().st_size
    if actual_bytes != expected_bytes:
        raise ValueError("selected backup byte count differs from bundle manifest")
    actual_digest = _sha256_file(backup_path)
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise ValueError("selected backup digest differs from bundle manifest")


def _verify_bundle_signature(
    manifest: dict[str, Any],
    *,
    signing_key: str | None,
    require_signature: bool,
) -> None:
    signature = manifest.get("signature")
    if signature is None:
        if require_signature:
            raise ValueError("bundle manifest is unsigned")
        return
    if not isinstance(signature, dict) or signature.get("algorithm") != "hmac-sha256":
        raise ValueError("unsupported bundle signature")
    if not signing_key:
        raise ValueError("signing key is required to verify bundle signature")
    expected = signature.get("value")
    if not isinstance(expected, str) or len(expected) != _SHA256_HEX_LENGTH:
        raise ValueError("bundle signature is missing or invalid")
    unsigned = dict(manifest)
    unsigned["signature"] = None
    payload = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    actual = hmac.new(signing_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise ValueError("bundle signature mismatch")


def _resolved_path(value: str) -> Path | None:
    try:
        return Path(value).resolve(strict=True)
    except OSError:
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
