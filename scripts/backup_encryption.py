"""Authenticated streaming encryption for PostgreSQL backup artifacts.

The format is intentionally small and self-identifying: a fixed magic header,
a random 96-bit nonce, ciphertext and the 128-bit AES-GCM authentication tag.
Keys are supplied out of band through a secret file or secret manager; they are
never written to backup reports or command lines.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"OBELISK-BACKUP-AES256GCM-V1\0"
NONCE_BYTES = 12
TAG_BYTES = 16
CHUNK_BYTES = 1024 * 1024


class BackupEncryptionError(ValueError):
    """A backup key or encrypted artifact cannot be safely processed."""


def parse_key(value: str) -> bytes:
    """Decode an URL-safe base64 256-bit backup key without retaining its text."""
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise BackupEncryptionError("backup encryption key must be URL-safe base64") from exc
    if len(key) != 32:
        raise BackupEncryptionError("backup encryption key must decode to exactly 32 bytes")
    return key


def key_fingerprint(key: bytes) -> str:
    """Return a non-secret identifier for reports and key-rotation evidence."""
    return hashlib.sha256(key).hexdigest()[:16]


def encrypt_file(source: Path, target: Path, key: bytes) -> dict[str, str | int]:
    """Encrypt *source* into *target* atomically using AES-256-GCM streaming."""
    nonce = os.urandom(NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    bytes_in = 0
    temporary = _temporary_target(target)
    try:
        with source.open("rb") as reader, temporary.open("wb") as writer:
            writer.write(MAGIC)
            writer.write(nonce)
            while chunk := reader.read(CHUNK_BYTES):
                bytes_in += len(chunk)
                writer.write(encryptor.update(chunk))
            writer.write(encryptor.finalize())
            writer.write(encryptor.tag)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "algorithm": "AES-256-GCM",
        "key_fingerprint": key_fingerprint(key),
        "plaintext_bytes": bytes_in,
        "ciphertext_sha256": _sha256_file(target),
    }


def decrypt_file(source: Path, target: Path, key: bytes) -> None:
    """Verify and decrypt an Obelisk encrypted artifact atomically into *target*."""
    size = source.stat().st_size
    minimum = len(MAGIC) + NONCE_BYTES + TAG_BYTES
    if size < minimum:
        raise BackupEncryptionError("encrypted backup is truncated")

    with source.open("rb") as reader:
        if reader.read(len(MAGIC)) != MAGIC:
            raise BackupEncryptionError("backup does not use the Obelisk encrypted format")
        nonce = reader.read(NONCE_BYTES)
        reader.seek(-TAG_BYTES, os.SEEK_END)
        tag = reader.read(TAG_BYTES)

    ciphertext_bytes = size - minimum
    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
    temporary = _temporary_target(target)
    try:
        with source.open("rb") as reader, temporary.open("wb") as writer:
            reader.seek(len(MAGIC) + NONCE_BYTES)
            remaining = ciphertext_bytes
            while remaining:
                chunk = reader.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise BackupEncryptionError("encrypted backup is truncated")
                remaining -= len(chunk)
                writer.write(decryptor.update(chunk))
            try:
                writer.write(decryptor.finalize())
            except InvalidTag as exc:
                raise BackupEncryptionError("encrypted backup authentication failed") from exc
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _temporary_target(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(descriptor)
    path = Path(name)
    path.chmod(0o600)
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        while chunk := artifact.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
