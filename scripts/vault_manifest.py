"""Manifest/checksum/signature helpers for Markdown vault bundles."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST_NAME = ".uam-vault-manifest.json"
CHECKSUM_NAME = ".uam-vault-manifest.sha256"
SIGNATURE_NAME = ".uam-vault-manifest.sig"
MANIFEST_FORMAT = "obelisk-vault-manifest-v1"
SIGNATURE_ALGORITHM = "hmac-sha256"


@dataclass(frozen=True, slots=True)
class VaultManifestVerification:
    """Result of verifying a materialized vault bundle."""

    file_count: int
    signed: bool


def write_vault_manifest(
    root: Path,
    *,
    tenant_id: str,
    workspace_id: str,
    signing_key: str | None = None,
) -> dict[str, Any]:
    """Write manifest, manifest checksum and optional signature for a vault."""
    manifest = {
        "format": MANIFEST_FORMAT,
        "created_at": datetime.now(UTC).isoformat(),
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "files": _file_entries(root),
    }
    payload = _canonical_json(manifest)
    manifest_path = root / MANIFEST_NAME
    manifest_path.write_text(payload, encoding="utf-8")
    checksum = _sha256_text(payload)
    (root / CHECKSUM_NAME).write_text(f"{checksum}  {MANIFEST_NAME}\n", encoding="utf-8")
    if signing_key:
        signature = _sign(payload, signing_key)
        (root / SIGNATURE_NAME).write_text(
            f"{SIGNATURE_ALGORITHM}:{signature}\n",
            encoding="utf-8",
        )
    return manifest


def verify_vault_manifest(
    root: Path,
    *,
    signing_key: str | None = None,
    require_signature: bool = False,
) -> VaultManifestVerification:
    """Verify a materialized vault manifest before import."""
    manifest_path = root / MANIFEST_NAME
    checksum_path = root / CHECKSUM_NAME
    signature_path = root / SIGNATURE_NAME
    if not manifest_path.exists():
        raise ValueError(f"missing vault manifest: {MANIFEST_NAME}")
    if not checksum_path.exists():
        raise ValueError(f"missing vault manifest checksum: {CHECKSUM_NAME}")

    payload = manifest_path.read_text(encoding="utf-8")
    expected_checksum = _read_checksum(checksum_path)
    actual_checksum = _sha256_text(payload)
    if not hmac.compare_digest(actual_checksum, expected_checksum):
        raise ValueError("vault manifest checksum mismatch")

    manifest = json.loads(payload)
    if manifest.get("format") != MANIFEST_FORMAT:
        raise ValueError(f"unsupported vault manifest format: {manifest.get('format')!r}")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("vault manifest missing files list")
    for entry in files:
        _verify_file_entry(root, entry)

    signed = signature_path.exists()
    if require_signature and not signed:
        raise ValueError(f"missing vault manifest signature: {SIGNATURE_NAME}")
    if signed or require_signature:
        if not signing_key:
            raise ValueError("vault manifest signature verification requires a signing key")
        signature = _read_signature(signature_path)
        expected_signature = _sign(payload, signing_key)
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError("vault manifest signature mismatch")
    return VaultManifestVerification(file_count=len(files), signed=signed)


def _file_entries(root: Path) -> list[dict[str, str | int]]:
    entries: list[dict[str, str | int]] = []
    for path in sorted(root.rglob("*.md")):
        relative = _safe_relative_path(root, path)
        data = path.read_bytes()
        entries.append(
            {
                "path": relative,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return entries


def _verify_file_entry(root: Path, entry: Any) -> None:
    if not isinstance(entry, dict):
        raise ValueError("vault manifest file entry must be an object")
    relative = str(entry.get("path", ""))
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise ValueError(f"unsafe vault manifest path: {relative!r}")
    path = root / relative
    if not path.exists():
        raise ValueError(f"vault manifest references missing file: {relative}")
    data = path.read_bytes()
    expected_size = int(entry.get("bytes", -1))
    if len(data) != expected_size:
        raise ValueError(f"vault file size mismatch: {relative}")
    expected_sha = str(entry.get("sha256", ""))
    actual_sha = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_sha, expected_sha):
        raise ValueError(f"vault file checksum mismatch: {relative}")


def _safe_relative_path(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe vault path: {path}")
    return relative.as_posix()


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sign(payload: str, signing_key: str) -> str:
    return hmac.new(
        signing_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _read_checksum(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("empty vault manifest checksum")
    return text.split()[0]


def _read_signature(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    prefix = f"{SIGNATURE_ALGORITHM}:"
    if not text.startswith(prefix):
        raise ValueError("unsupported vault manifest signature algorithm")
    return text[len(prefix) :]
