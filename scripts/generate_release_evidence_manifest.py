"""Seal production reports into a signed, content-addressed release manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from verify_release_evidence import (
    MANIFEST_FORMAT,
    REQUIRED_ARTIFACTS,
    SIGNATURE_ALGORITHM,
    sign_manifest,
)

DEFAULT_ARTIFACT_PATHS = {
    "agent_soak": "ops/agent-soak.json",
    "conversation_pipeline": "ops/conversation-pipeline.json",
    "embedding": "ops/embedding.json",
    "memory_llm": "ops/memory-llm.json",
    "load_smoke": "ops/load-smoke.json",
    "metrics_health": "ops/metrics-health.json",
    "ops_schedule": "ops/ops-schedule.json",
    "observability": "ops/observability-preflight.json",
    "release_notes": "ops/release-notes.json",
    "scheduled_backup": "backups/latest-backup-report.json",
    "audit_retention": "ops/audit-retention.json",
    "deployment_preflight": "ops/deployment-preflight.json",
    "secret_files": "ops/secret-files.json",
    "vault_import": "ops/vault-import.json",
    "branch_protection": "ops/branch-protection.json",
    "ui_walkthrough": "ops/ui-walkthrough.json",
}


def main() -> int:
    """Write a complete signed release-evidence manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, help="Release identifier.")
    parser.add_argument(
        "--source-commit",
        default=None,
        help="Exact 40-character source commit; defaults to git HEAD.",
    )
    parser.add_argument(
        "--image-digest",
        required=True,
        help="Immutable OCI digest in sha256:<64 hex> form.",
    )
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument(
        "--api-url",
        required=True,
        help="Exact server URL used by agent/load/UI release checks.",
    )
    parser.add_argument(
        "--public-url",
        required=True,
        help="Externally exposed HTTPS URL verified by deployment preflight.",
    )
    parser.add_argument(
        "--signing-key-id",
        required=True,
        help="Non-secret identifier for the operator-held HMAC key.",
    )
    parser.add_argument(
        "--signing-key-file",
        type=Path,
        help=(
            "File containing the release HMAC key; defaults to "
            "UAM_RELEASE_SIGNING_KEY or UAM_RELEASE_SIGNING_KEY_FILE."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("release-evidence.json"),
        help="Manifest path to write.",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Override one artifact path.",
    )
    args = parser.parse_args()

    source_commit = args.source_commit or _git_head()
    signing_key = _read_signing_key(args.signing_key_file)
    if not signing_key or len(signing_key) < 32:
        raise SystemExit("release signing key must contain at least 32 characters")
    artifact_paths = build_artifacts(tuple(args.artifact))
    manifest = build_manifest(
        release=args.release,
        source_commit=source_commit,
        image_digest=args.image_digest,
        deployment_id=args.deployment_id,
        api_url=args.api_url,
        public_url=args.public_url,
        signing_key_id=args.signing_key_id,
        signing_key=signing_key,
        output_path=args.output,
        artifacts=artifact_paths,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"release_evidence_manifest={args.output}")
    return 0


def build_artifacts(overrides: tuple[str, ...] = ()) -> dict[str, str]:
    """Return complete artifact mapping with optional NAME=PATH overrides."""
    artifacts = dict(DEFAULT_ARTIFACT_PATHS)
    missing = sorted(REQUIRED_ARTIFACTS - set(artifacts))
    extra = sorted(set(artifacts) - REQUIRED_ARTIFACTS)
    if missing or extra:
        raise RuntimeError(
            "default artifact paths must match REQUIRED_ARTIFACTS; "
            f"missing={missing}, extra={extra}"
        )
    for override in overrides:
        if "=" not in override:
            raise ValueError(f"artifact override must use NAME=PATH: {override!r}")
        name, path = (part.strip() for part in override.split("=", 1))
        if name not in REQUIRED_ARTIFACTS:
            raise ValueError(f"unknown artifact name: {name!r}")
        if not path:
            raise ValueError(f"artifact path for {name!r} must not be empty")
        artifacts[name] = path
    return dict(sorted(artifacts.items()))


def build_manifest(
    *,
    release: str,
    source_commit: str,
    image_digest: str,
    deployment_id: str,
    api_url: str,
    public_url: str,
    signing_key_id: str,
    signing_key: str,
    output_path: Path,
    artifacts: dict[str, str],
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Build and sign a v2 manifest after hashing every artifact."""
    if not release.strip():
        raise ValueError("release must not be empty")
    if re.fullmatch(r"[0-9a-fA-F]{40}", source_commit.strip()) is None:
        raise ValueError("source_commit must be a 40-character Git commit")
    if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", image_digest.strip()) is None:
        raise ValueError("image_digest must use sha256:<64 hex>")
    if not deployment_id.strip():
        raise ValueError("deployment_id must not be empty")
    if not signing_key_id.strip():
        raise ValueError("signing_key_id must not be empty")
    if len(signing_key) < 32:
        raise ValueError("release signing key must contain at least 32 characters")
    if not _valid_url(api_url, https_only=False):
        raise ValueError("api_url must be an HTTP(S) URL without credentials")
    if not _valid_url(public_url, https_only=True):
        raise ValueError("public_url must be an HTTPS URL without credentials")

    base = output_path.resolve().parent
    entries: dict[str, dict[str, str]] = {}
    if set(artifacts) != REQUIRED_ARTIFACTS:
        raise ValueError("artifact mapping must contain the exact required artifact set")
    for name, raw_path in sorted(artifacts.items()):
        relative_path = Path(raw_path)
        if relative_path.is_absolute():
            raise ValueError(f"artifact {name!r} path must be relative")
        artifact_path = (base / relative_path).resolve()
        if not artifact_path.is_relative_to(base):
            raise ValueError(f"artifact {name!r} path escapes the release bundle")
        if not artifact_path.is_file():
            raise FileNotFoundError(f"release artifact not found: {artifact_path}")
        entries[name] = {
            "path": relative_path.as_posix(),
            "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        }

    models = _model_targets(base, entries)

    timestamp = (generated_at or datetime.now(UTC)).astimezone(UTC)
    manifest: dict[str, object] = {
        "format": MANIFEST_FORMAT,
        "release": release.strip(),
        "generated_at": timestamp.isoformat().replace("+00:00", "Z"),
        "source_commit": source_commit.strip().lower(),
        "image_digest": image_digest.strip().lower(),
        "target": {
            "deployment_id": deployment_id.strip(),
            "api_url": api_url.rstrip("/"),
            "public_url": public_url.rstrip("/"),
        },
        "models": models,
        "artifacts": entries,
    }
    signature: dict[str, str] = {
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": signing_key_id.strip(),
    }
    manifest["signature"] = signature
    signature["value"] = sign_manifest(manifest, signing_key)
    return manifest


def _model_targets(
    base: Path,
    entries: dict[str, dict[str, str]],
) -> dict[str, dict[str, object]]:
    """Read non-secret provider identities from the sealed live reports."""
    embedding = _read_report(base, entries["embedding"]["path"])
    memory_llm = _read_report(base, entries["memory_llm"]["path"])
    embedding_dimension = embedding.get("dimension")
    if (
        not isinstance(embedding_dimension, int)
        or isinstance(embedding_dimension, bool)
        or embedding_dimension <= 0
    ):
        raise ValueError("embedding report dimension must be a positive integer")
    llm_fingerprint = memory_llm.get("config_fingerprint")
    if (
        not isinstance(llm_fingerprint, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", llm_fingerprint) is None
    ):
        raise ValueError("memory LLM report config_fingerprint must be sha256 hex")
    return {
        "embedding": {
            "provider": _report_text(embedding, "provider", "embedding"),
            "base_url": _report_url(embedding, "embedding"),
            "model": _report_text(embedding, "model", "embedding"),
            "dimension": embedding_dimension,
        },
        "memory_llm": {
            "provider": _report_text(memory_llm, "provider", "memory LLM"),
            "base_url": _report_url(memory_llm, "memory LLM"),
            "model": _report_text(memory_llm, "model", "memory LLM"),
            "config_fingerprint": llm_fingerprint.lower(),
        },
    }


def _read_report(base: Path, relative_path: str) -> dict[str, Any]:
    payload = json.loads((base / relative_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"release report {relative_path!r} must be a JSON object")
    return payload


def _report_text(report: dict[str, Any], field: str, label: str) -> str:
    value = report.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} report {field} must be a non-empty string")
    return value.strip()


def _report_url(report: dict[str, Any], label: str) -> str:
    value = _report_text(report, "base_url", label)
    if not _valid_url(value, https_only=False):
        raise ValueError(f"{label} report base_url must be a safe HTTP(S) URL")
    return value.rstrip("/")


def _read_signing_key(path: Path | None) -> str | None:
    if path is not None:
        return path.read_text(encoding="utf-8").strip() or None
    direct = os.getenv("UAM_RELEASE_SIGNING_KEY", "").strip()
    if direct:
        return direct
    configured_path = (
        Path(os.environ["UAM_RELEASE_SIGNING_KEY_FILE"])
        if os.getenv("UAM_RELEASE_SIGNING_KEY_FILE")
        else None
    )
    if configured_path is None:
        return None
    return configured_path.read_text(encoding="utf-8").strip() or None


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _valid_url(value: str, *, https_only: bool) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    schemes = {"https"} if https_only else {"http", "https"}
    return (
        parsed.scheme.lower() in schemes
        and bool(parsed.netloc)
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and "@" not in parsed.netloc
        and not parsed.query
        and not parsed.fragment
    )


if __name__ == "__main__":
    raise SystemExit(main())
