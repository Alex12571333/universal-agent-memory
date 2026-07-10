"""Verify release evidence artifacts for a full-production Obelisk Memory release."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MANIFEST_FORMAT = "obelisk-release-evidence-manifest-v2"
SIGNATURE_ALGORITHM = "hmac-sha256"
DEFAULT_MAX_AGE_HOURS = 24.0
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
_IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-fA-F]{64}")

REQUIRED_ARTIFACTS = {
    "agent_soak",
    "conversation_pipeline",
    "embedding",
    "memory_llm",
    "load_smoke",
    "metrics_health",
    "ops_schedule",
    "observability",
    "release_notes",
    "scheduled_backup",
    "audit_retention",
    "deployment_preflight",
    "secret_files",
    "vault_import",
    "branch_protection",
    "ui_walkthrough",
}


@dataclass(frozen=True, slots=True)
class EvidenceCheck:
    """One release evidence verification result."""

    name: str
    passed: bool
    detail: str


def main() -> int:
    """Verify a release evidence manifest and its referenced JSON reports."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        type=Path,
        help="Path to signed obelisk-release-evidence-manifest-v2 JSON",
    )
    parser.add_argument(
        "--signing-key-file",
        type=Path,
        help=(
            "File containing the release evidence HMAC key; defaults to "
            "UAM_RELEASE_SIGNING_KEY or UAM_RELEASE_SIGNING_KEY_FILE."
        ),
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        help="Maximum manifest age at release time; use 0 for archival verification.",
    )
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--expected-image-digest")
    parser.add_argument("--expected-deployment-id")
    parser.add_argument("--json", action="store_true", help="Print JSON result")
    args = parser.parse_args()

    signing_key = _read_signing_key(args.signing_key_file)
    checks = verify_manifest(
        args.manifest,
        signing_key=signing_key,
        max_age_hours=args.max_age_hours,
        expected_source_commit=args.expected_source_commit,
        expected_image_digest=args.expected_image_digest,
        expected_deployment_id=args.expected_deployment_id,
    )
    passed = all(check.passed for check in checks)
    if args.json:
        print(
            json.dumps(
                {
                    "passed": passed,
                    "checks": [asdict(check) for check in checks],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for check in checks:
            status = "PASS" if check.passed else "FAIL"
            print(f"{status} {check.name}: {check.detail}")
        print("release_evidence=PASS" if passed else "release_evidence=FAIL")
    return 0 if passed else 1


def verify_manifest(
    path: Path,
    *,
    signing_key: str | None = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
    expected_source_commit: str | None = None,
    expected_image_digest: str | None = None,
    expected_deployment_id: str | None = None,
) -> list[EvidenceCheck]:
    """Return release evidence checks for a manifest path."""
    checks: list[EvidenceCheck] = []
    try:
        manifest = _read_json(path)
    except Exception as exc:  # noqa: BLE001 - CLI reports malformed evidence.
        return [EvidenceCheck("manifest:read", False, f"{type(exc).__name__}: {exc}")]

    checks.append(
        EvidenceCheck(
            "manifest:format",
            manifest.get("format") == MANIFEST_FORMAT,
            (
                f"format={MANIFEST_FORMAT}"
                if manifest.get("format") == MANIFEST_FORMAT
                else f"expected {MANIFEST_FORMAT}, got {manifest.get('format')!r}"
            ),
        )
    )

    release = manifest.get("release")
    checks.append(
        EvidenceCheck(
            "manifest:release",
            isinstance(release, str) and bool(release.strip()),
            (
                f"release={release}"
                if isinstance(release, str) and release.strip()
                else "release missing"
            ),
        )
    )
    checks.extend(
        _verify_manifest_identity(
            manifest,
            max_age_hours=max_age_hours,
            now=now or datetime.now(UTC),
            expected_source_commit=expected_source_commit,
            expected_image_digest=expected_image_digest,
            expected_deployment_id=expected_deployment_id,
        )
    )
    checks.extend(_verify_manifest_signature(manifest, signing_key))

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        checks.append(EvidenceCheck("manifest:artifacts", False, "artifacts must be an object"))
        return checks
    artifact_names = set(artifacts)
    missing = sorted(REQUIRED_ARTIFACTS - artifact_names)
    extra = sorted(artifact_names - REQUIRED_ARTIFACTS)
    checks.append(
        EvidenceCheck(
            "manifest:required-artifacts",
            not missing and not extra,
            (
                "exact required artifact set listed"
                if not missing and not extra
                else "; ".join(
                    part
                    for part in (
                        "missing artifacts: " + ", ".join(missing) if missing else "",
                        "unexpected artifacts: " + ", ".join(extra) if extra else "",
                    )
                    if part
                )
            ),
        )
    )

    base = path.resolve().parent
    readers = {
        "agent_soak": _verify_agent_soak,
        "conversation_pipeline": _verify_conversation_pipeline,
        "embedding": _verify_embedding,
        "memory_llm": _verify_memory_llm,
        "load_smoke": _verify_load_smoke,
        "metrics_health": _verify_metrics_health,
        "ops_schedule": _verify_ops_schedule,
        "observability": _verify_observability,
        "release_notes": _verify_release_notes,
        "scheduled_backup": _verify_scheduled_backup,
        "audit_retention": _verify_audit_retention,
        "deployment_preflight": _verify_deployment_preflight,
        "secret_files": _verify_secret_files,
        "vault_import": _verify_vault_import,
        "branch_protection": _verify_branch_protection,
        "ui_walkthrough": _verify_ui_walkthrough,
    }
    payloads: dict[str, dict[str, Any]] = {}
    for name, verifier in readers.items():
        entry = artifacts.get(name)
        try:
            artifact_path, expected_sha256 = _artifact_entry(base, entry)
        except Exception as exc:  # noqa: BLE001 - keep checking all artifacts.
            checks.append(
                EvidenceCheck(
                    f"{name}:path",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        try:
            artifact_bytes = artifact_path.read_bytes()
        except Exception as exc:  # noqa: BLE001 - keep checking all artifacts.
            checks.append(
                EvidenceCheck(
                    f"{name}:read",
                    False,
                    f"{artifact_path}: {type(exc).__name__}: {exc}",
                )
            )
            continue
        actual_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        checksum_ok = hmac.compare_digest(actual_sha256, expected_sha256)
        checks.append(
            EvidenceCheck(
                f"{name}:sha256",
                checksum_ok,
                "artifact checksum verified" if checksum_ok else "artifact checksum mismatch",
            )
        )
        try:
            payload = _decode_json_object(artifact_bytes)
        except Exception as exc:  # noqa: BLE001 - keep checking all artifacts.
            checks.append(
                EvidenceCheck(
                    f"{name}:read",
                    False,
                    f"{artifact_path}: {type(exc).__name__}: {exc}",
                )
            )
            continue
        payloads[name] = payload
        checks.extend(verifier(payload))
    checks.extend(
        _verify_cross_artifact_identity(
            manifest,
            payloads,
            max_age_hours=max_age_hours,
        )
    )
    return checks


def _artifact_entry(base: Path, entry: object) -> tuple[Path, str]:
    if not isinstance(entry, dict):
        raise ValueError("artifact entry must be an object with path and sha256")
    raw_path = entry.get("path")
    expected_sha256 = entry.get("sha256")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("artifact path must be a non-empty string")
    if Path(raw_path).is_absolute():
        raise ValueError("artifact path must be relative to the release bundle")
    path = (base / raw_path).resolve()
    if not path.is_relative_to(base):
        raise ValueError("artifact path escapes the release bundle")
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", expected_sha256
    ):
        raise ValueError("artifact sha256 must be a 64-character hex digest")
    return path, expected_sha256.lower()


def _read_json(path: Path) -> dict[str, Any]:
    return _decode_json_object(path.read_bytes())


def _decode_json_object(data: bytes) -> dict[str, Any]:
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    return payload


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Return stable bytes signed by the release operator."""
    unsigned = dict(manifest)
    signature = manifest.get("signature")
    if isinstance(signature, dict):
        unsigned["signature"] = {key: value for key, value in signature.items() if key != "value"}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_manifest(manifest: dict[str, Any], signing_key: str) -> str:
    """Return HMAC-SHA256 for a canonical release evidence manifest."""
    return hmac.new(
        signing_key.encode("utf-8"),
        canonical_manifest_bytes(manifest),
        hashlib.sha256,
    ).hexdigest()


def _read_signing_key(path: Path | None = None) -> str | None:
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


def _verify_manifest_signature(
    manifest: dict[str, Any], signing_key: str | None
) -> list[EvidenceCheck]:
    signature = manifest.get("signature")
    if not isinstance(signature, dict):
        return [EvidenceCheck("manifest:signature", False, "signature object missing")]
    algorithm = signature.get("algorithm")
    value = signature.get("value")
    key_id = signature.get("key_id")
    checks = [
        EvidenceCheck(
            "manifest:signature-algorithm",
            algorithm == SIGNATURE_ALGORITHM,
            f"algorithm={algorithm!r}",
        ),
        EvidenceCheck(
            "manifest:signature-key-id",
            isinstance(key_id, str) and bool(key_id.strip()),
            f"key_id={key_id!r}",
        ),
        EvidenceCheck(
            "manifest:signing-key",
            bool(signing_key) and len(signing_key or "") >= 32,
            "operator signing key configured" if signing_key else "operator signing key missing",
        ),
    ]
    valid = False
    if (
        algorithm == SIGNATURE_ALGORITHM
        and isinstance(value, str)
        and signing_key
        and len(signing_key) >= 32
    ):
        valid = hmac.compare_digest(value, sign_manifest(manifest, signing_key))
    checks.append(
        EvidenceCheck(
            "manifest:signature",
            valid,
            "manifest signature verified" if valid else "manifest signature mismatch",
        )
    )
    return checks


def _verify_manifest_identity(
    manifest: dict[str, Any],
    *,
    max_age_hours: float,
    now: datetime,
    expected_source_commit: str | None,
    expected_image_digest: str | None,
    expected_deployment_id: str | None,
) -> list[EvidenceCheck]:
    source_commit = manifest.get("source_commit")
    image_digest = manifest.get("image_digest")
    target = manifest.get("target")
    max_age_valid = math.isfinite(max_age_hours) and max_age_hours >= 0
    checks = [
        EvidenceCheck(
            "manifest:max-age",
            max_age_valid,
            (
                f"max_age_hours={max_age_hours}"
                if max_age_valid
                else "max_age_hours must be finite and non-negative"
            ),
        ),
        EvidenceCheck(
            "manifest:max-age-policy",
            max_age_hours >= 0,
            f"max_age_hours={max_age_hours}",
        ),
        EvidenceCheck(
            "manifest:source-commit",
            isinstance(source_commit, str) and _COMMIT_RE.fullmatch(source_commit) is not None,
            f"source_commit={source_commit!r}",
        ),
        EvidenceCheck(
            "manifest:image-digest",
            isinstance(image_digest, str) and _IMAGE_DIGEST_RE.fullmatch(image_digest) is not None,
            f"image_digest={image_digest!r}",
        ),
    ]
    if expected_source_commit is not None:
        checks.append(
            EvidenceCheck(
                "manifest:expected-source-commit",
                source_commit == expected_source_commit,
                f"expected={expected_source_commit!r} actual={source_commit!r}",
            )
        )
    if expected_image_digest is not None:
        checks.append(
            EvidenceCheck(
                "manifest:expected-image-digest",
                image_digest == expected_image_digest,
                f"expected={expected_image_digest!r} actual={image_digest!r}",
            )
        )

    if not isinstance(target, dict):
        checks.append(EvidenceCheck("manifest:target", False, "target object missing"))
    else:
        deployment_id = target.get("deployment_id")
        api_url = target.get("api_url")
        public_url = target.get("public_url")
        checks.extend(
            [
                EvidenceCheck(
                    "manifest:deployment-id",
                    isinstance(deployment_id, str) and bool(deployment_id.strip()),
                    f"deployment_id={deployment_id!r}",
                ),
                EvidenceCheck(
                    "manifest:api-url",
                    _valid_http_url(api_url, require_https=False),
                    f"api_url={api_url!r}",
                ),
                EvidenceCheck(
                    "manifest:public-url",
                    _valid_http_url(public_url, require_https=True),
                    f"public_url={public_url!r}",
                ),
            ]
        )
        if expected_deployment_id is not None:
            checks.append(
                EvidenceCheck(
                    "manifest:expected-deployment-id",
                    deployment_id == expected_deployment_id,
                    f"expected={expected_deployment_id!r} actual={deployment_id!r}",
                )
            )

    generated_at = manifest.get("generated_at")
    generated: datetime | None = None
    try:
        if not isinstance(generated_at, str):
            raise ValueError("generated_at must be a string")
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if generated.tzinfo is None:
            raise ValueError("generated_at must include timezone")
        generated = generated.astimezone(UTC)
    except ValueError as exc:
        checks.append(EvidenceCheck("manifest:generated-at", False, str(exc)))
    else:
        checks.append(
            EvidenceCheck(
                "manifest:generated-at",
                True,
                f"generated_at={generated.isoformat()}",
            )
        )
        future_ok = generated <= now.astimezone(UTC) + timedelta(minutes=5)
        checks.append(
            EvidenceCheck(
                "manifest:not-from-future",
                future_ok,
                "timestamp is plausible" if future_ok else "timestamp is in the future",
            )
        )
        if max_age_valid and max_age_hours > 0:
            age = now.astimezone(UTC) - generated
            checks.append(
                EvidenceCheck(
                    "manifest:freshness",
                    timedelta(0) <= age <= timedelta(hours=max_age_hours),
                    (
                        f"age_hours={max(0.0, age.total_seconds() / 3600):.2f} "
                        f"limit={max_age_hours:.2f}"
                    ),
                )
            )
    return checks


def _verify_cross_artifact_identity(
    manifest: dict[str, Any],
    payloads: dict[str, dict[str, Any]],
    *,
    max_age_hours: float,
) -> list[EvidenceCheck]:
    checks: list[EvidenceCheck] = []
    release_notes = payloads.get("release_notes", {})
    checks.extend(
        [
            EvidenceCheck(
                "identity:release-notes-release",
                release_notes.get("release") == manifest.get("release"),
                "release notes match manifest release",
            ),
            EvidenceCheck(
                "identity:release-notes-commit",
                release_notes.get("current_commit") == manifest.get("source_commit"),
                "release notes match manifest source commit",
            ),
        ]
    )
    raw_target = manifest.get("target")
    target: dict[str, Any] = raw_target if isinstance(raw_target, dict) else {}
    api_url = target.get("api_url")
    runtime_reports = (
        "agent_soak",
        "conversation_pipeline",
        "load_smoke",
        "ui_walkthrough",
    )
    runtime_builds: list[tuple[str, str]] = []
    manifest_generated = _aware_datetime(manifest.get("generated_at"))
    valid_max_age = math.isfinite(max_age_hours) and max_age_hours >= 0
    for name in runtime_reports:
        payload = payloads.get(name, {})
        checks.append(
            EvidenceCheck(
                f"identity:{name}-target",
                _normalize_url(payload.get("base_url")) == _normalize_url(api_url),
                f"report={payload.get('base_url')!r} target={api_url!r}",
            )
        )
        build_ok, build_detail, build_version, build_time = _match_runtime_build(
            payload.get("build"),
            manifest,
            target,
        )
        checks.append(
            EvidenceCheck(
                f"identity:{name}-build",
                build_ok,
                build_detail,
            )
        )
        if build_ok:
            runtime_builds.append((build_version, build_time))

        report_generated = _aware_datetime(payload.get("generated_at"))
        freshness_ok = report_generated is not None and manifest_generated is not None
        freshness_detail = "generated_at missing or lacks timezone"
        if freshness_ok and report_generated is not None and manifest_generated is not None:
            age = manifest_generated - report_generated
            freshness_ok = valid_max_age and age >= -timedelta(minutes=5)
            if freshness_ok and max_age_hours > 0:
                freshness_ok = age <= timedelta(hours=max_age_hours)
            freshness_detail = (
                f"report={report_generated.isoformat()} manifest={manifest_generated.isoformat()}"
            )
        checks.append(
            EvidenceCheck(
                f"identity:{name}-freshness",
                freshness_ok,
                freshness_detail,
            )
        )

    all_builds_present = len(runtime_builds) == len(runtime_reports)
    consistent_build = all_builds_present and len(set(runtime_builds)) == 1
    checks.append(
        EvidenceCheck(
            "identity:runtime-build-consistency",
            consistent_build,
            (
                "all live reports exercised one version/build time"
                if consistent_build
                else f"runtime builds={runtime_builds!r}"
            ),
        )
    )
    raw_models = manifest.get("models")
    models: dict[str, Any] = raw_models if isinstance(raw_models, dict) else {}
    embedding_report = payloads.get("embedding", {})
    embedding_target = models.get("embedding")
    embedding_matches = isinstance(embedding_target, dict) and (
        embedding_target.get("provider") == embedding_report.get("provider")
        and _normalize_url(embedding_target.get("base_url"))
        == _normalize_url(embedding_report.get("base_url"))
        and embedding_target.get("model") == embedding_report.get("model")
        and embedding_target.get("dimension") == embedding_report.get("dimension")
    )
    checks.append(
        EvidenceCheck(
            "identity:embedding-model",
            embedding_matches,
            f"manifest={embedding_target!r} report={_model_report_view(embedding_report)!r}",
        )
    )
    llm_report = payloads.get("memory_llm", {})
    llm_target = models.get("memory_llm")
    llm_matches = isinstance(llm_target, dict) and (
        llm_target.get("provider") == llm_report.get("provider")
        and _normalize_url(llm_target.get("base_url")) == _normalize_url(llm_report.get("base_url"))
        and llm_target.get("model") == llm_report.get("model")
        and llm_target.get("config_fingerprint") == llm_report.get("config_fingerprint")
    )
    checks.append(
        EvidenceCheck(
            "identity:memory-llm-model",
            llm_matches,
            f"manifest={llm_target!r} report={_model_report_view(llm_report)!r}",
        )
    )
    for name, payload in (
        ("embedding", embedding_report),
        ("memory_llm", llm_report),
    ):
        generated = _aware_datetime(payload.get("generated_at"))
        fresh = generated is not None and manifest_generated is not None and valid_max_age
        detail = "generated_at missing or lacks timezone"
        if fresh and generated is not None and manifest_generated is not None:
            age = manifest_generated - generated
            fresh = age >= -timedelta(minutes=5)
            if fresh and max_age_hours > 0:
                fresh = age <= timedelta(hours=max_age_hours)
            detail = f"report={generated.isoformat()} manifest={manifest_generated.isoformat()}"
        checks.append(EvidenceCheck(f"identity:{name}-freshness", fresh, detail))

    deployment = payloads.get("deployment_preflight", {})
    checks.append(
        EvidenceCheck(
            "identity:deployment-public-url",
            _normalize_url(deployment.get("public_url"))
            == _normalize_url(target.get("public_url")),
            f"report={deployment.get('public_url')!r} target={target.get('public_url')!r}",
        )
    )
    return checks


def _match_runtime_build(
    build: object,
    manifest: dict[str, Any],
    target: dict[str, Any],
) -> tuple[bool, str, str, str]:
    """Match one live report's immutable runtime identity to the manifest."""
    if not isinstance(build, dict):
        return False, "build identity missing", "", ""
    version = build.get("version")
    source_commit = build.get("source_commit")
    image_digest = build.get("image_digest")
    deployment_id = build.get("deployment_id")
    parsed_build_time = _aware_datetime(build.get("build_time"))
    valid = (
        isinstance(version, str)
        and bool(version.strip())
        and isinstance(source_commit, str)
        and _COMMIT_RE.fullmatch(source_commit) is not None
        and source_commit == manifest.get("source_commit")
        and isinstance(image_digest, str)
        and _IMAGE_DIGEST_RE.fullmatch(image_digest) is not None
        and image_digest == manifest.get("image_digest")
        and isinstance(deployment_id, str)
        and deployment_id == target.get("deployment_id")
        and parsed_build_time is not None
    )
    detail = (
        f"source={source_commit!r} image={image_digest!r} "
        f"deployment={deployment_id!r} version={version!r}"
    )
    return (
        valid,
        detail,
        version.strip() if isinstance(version, str) else "",
        parsed_build_time.isoformat() if parsed_build_time is not None else "",
    )


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _model_report_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only non-secret model identity fields for verifier diagnostics."""
    return {
        key: payload.get(key)
        for key in (
            "provider",
            "base_url",
            "model",
            "dimension",
            "config_fingerprint",
        )
        if key in payload
    }


def _valid_http_url(value: object, *, require_https: bool) -> bool:
    if not isinstance(value, str):
        return False
    try:
        split = urlsplit(value)
    except ValueError:
        return False
    allowed = {"https"} if require_https else {"http", "https"}
    return (
        split.scheme.lower() in allowed
        and bool(split.netloc)
        and split.hostname is not None
        and split.username is None
        and split.password is None
        and "@" not in split.netloc
        and not split.query
        and not split.fragment
    )


def _normalize_url(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or not _valid_http_url(value, require_https=False)
    ):
        return None
    split = urlsplit(value.strip())
    if split.scheme.lower() not in {"http", "https"} or not split.netloc:
        return None
    path = split.path.rstrip("/")
    return urlunsplit((split.scheme.lower(), split.netloc.lower(), path, "", ""))


def _verify_agent_soak(payload: dict[str, Any]) -> list[EvidenceCheck]:
    checks = [
        _format_check("agent_soak", payload, "obelisk-agent-soak-v1"),
        _ok_check("agent_soak", payload),
    ]
    names = _check_names(payload)
    required = {
        "build-identity",
        "health",
        "cross-workspace-leakage",
    }
    checks.append(
        EvidenceCheck(
            "agent_soak:openclaw",
            any(name.startswith("openclaw:recall:") for name in names),
            "OpenClaw recall lifecycle evidence present",
        )
    )
    checks.append(
        EvidenceCheck(
            "agent_soak:hermes",
            any(name.startswith("hermes:recall:") for name in names),
            "Hermes recall lifecycle evidence present",
        )
    )
    missing = sorted(required - names)
    checks.append(
        EvidenceCheck(
            "agent_soak:required-checks",
            not missing,
            "required checks present" if not missing else "missing: " + ", ".join(missing),
        )
    )
    return checks


def _verify_memory_llm(payload: dict[str, Any]) -> list[EvidenceCheck]:
    names = _check_names(payload)
    required = {"chat-completions", "json-memory-curation"}
    missing = sorted(required - names)
    return [
        _format_check("memory_llm", payload, "obelisk-memory-llm-eval-v1"),
        _ok_check("memory_llm", payload),
        EvidenceCheck(
            "memory_llm:required-checks",
            not missing,
            "required checks present" if not missing else "missing: " + ", ".join(missing),
        ),
    ]


def _verify_conversation_pipeline(payload: dict[str, Any]) -> list[EvidenceCheck]:
    names = _check_names(payload)
    required = {
        "build-identity",
        "raw-turn-stored",
        "raw-turn-listed",
        "raw-turn-not-recalled",
        "curation-created-memory",
        "curated-memory-recalled",
    }
    missing = sorted(required - names)
    return [
        _format_check(
            "conversation_pipeline",
            payload,
            "obelisk-conversation-pipeline-v1",
        ),
        _ok_check("conversation_pipeline", payload),
        EvidenceCheck(
            "conversation_pipeline:required-checks",
            not missing,
            "required checks present" if not missing else "missing: " + ", ".join(missing),
        ),
        EvidenceCheck(
            "conversation_pipeline:turn-and-memory",
            bool(payload.get("turn_id")) and bool(payload.get("memory_id")),
            "raw turn and curated memory ids present",
        ),
    ]


def _verify_embedding(payload: dict[str, Any]) -> list[EvidenceCheck]:
    names = _check_names(payload)
    required = {
        "endpoint-reachable",
        "dimension",
        "semantic:storage routing",
        "semantic:production embedding model",
        "semantic:openclaw integration",
        "semantic:hermes integration",
        "semantic:freshness preference",
    }
    missing = sorted(required - names)
    return [
        _format_check("embedding", payload, "obelisk-embedding-eval-v1"),
        _ok_check("embedding", payload),
        EvidenceCheck(
            "embedding:required-checks",
            not missing,
            "required checks present" if not missing else "missing: " + ", ".join(missing),
        ),
    ]


def _verify_metrics_health(payload: dict[str, Any]) -> list[EvidenceCheck]:
    names = _check_names(payload)
    required = {
        "outbox_pending_total",
        "outbox_dead_letter_total",
        "outbox_lag_seconds",
        "processed_events_inflight_total",
    }
    missing = sorted(required - names)
    return [
        _format_check("metrics_health", payload, "obelisk-metrics-health-v1"),
        _ok_check("metrics_health", payload),
        EvidenceCheck(
            "metrics_health:required-checks",
            not missing,
            "required checks present" if not missing else "missing: " + ", ".join(missing),
        ),
    ]


def _verify_ops_schedule(payload: dict[str, Any]) -> list[EvidenceCheck]:
    names = _check_names(payload)
    required = {
        "backup-schedule:file-exists",
        "backup-schedule:required-command",
        "audit-retention-schedule:file-exists",
        "audit-retention-schedule:required-command",
        "metrics-schedule:file-exists",
        "metrics-schedule:required-command",
        "UAM_BACKUP_ALERT_WEBHOOK:configured",
        "UAM_METRICS_ALERT_WEBHOOK:configured",
        "backup-artifact-root:durable-prefix",
        "audit-artifact-root:durable-prefix",
    }
    missing = sorted(required - names)
    return [
        _format_check("ops_schedule", payload, "obelisk-ops-schedule-preflight-v1"),
        _ok_check("ops_schedule", payload),
        EvidenceCheck(
            "ops_schedule:required-checks",
            not missing,
            "required checks present" if not missing else "missing: " + ", ".join(missing),
        ),
    ]


def _verify_observability(payload: dict[str, Any]) -> list[EvidenceCheck]:
    names = _check_names(payload)
    required = {
        "grafana-dashboard:json-valid",
        "grafana-dashboard:required-metrics",
        "prometheus-alerts:required-alerts",
        "prometheus-alerts:required-metrics",
        "prometheus-alerts:production-group",
    }
    missing = sorted(required - names)
    return [
        _format_check("observability", payload, "obelisk-observability-preflight-v1"),
        _ok_check("observability", payload),
        EvidenceCheck(
            "observability:required-checks",
            not missing,
            "required checks present" if not missing else "missing: " + ", ".join(missing),
        ),
    ]


def _verify_load_smoke(payload: dict[str, Any]) -> list[EvidenceCheck]:
    names = _check_names(payload)
    required = {
        "build-identity",
        "health",
        "concurrent-retain-recall",
        "error-rate",
        "retain-p95",
        "recall-p95",
        "metrics-backlog",
    }
    missing = sorted(required - names)
    return [
        _format_check("load_smoke", payload, "obelisk-load-smoke-v1"),
        _ok_check("load_smoke", payload),
        EvidenceCheck(
            "load_smoke:required-checks",
            not missing,
            "required checks present" if not missing else "missing: " + ", ".join(missing),
        ),
        EvidenceCheck(
            "load_smoke:parallelism",
            int(payload.get("agents") or 0) >= 2 and int(payload.get("total_operations") or 0) >= 4,
            "parallel load evidence present",
        ),
    ]


def _verify_release_notes(payload: dict[str, Any]) -> list[EvidenceCheck]:
    rollback = payload.get("rollback")
    changelog = payload.get("changelog")
    rollback_text = " ".join(str(item).lower() for item in rollback or [])
    return [
        _format_check("release_notes", payload, "obelisk-release-notes-v1"),
        _ok_check("release_notes", payload),
        EvidenceCheck(
            "release_notes:changelog",
            isinstance(changelog, list) and bool(changelog),
            "versioned changelog present",
        ),
        EvidenceCheck(
            "release_notes:rollback",
            isinstance(rollback, list)
            and len(rollback) >= 4
            and "restore" in rollback_text
            and "previous" in rollback_text,
            "rollback instructions include previous ref and restore guidance",
        ),
    ]


def _verify_scheduled_backup(payload: dict[str, Any]) -> list[EvidenceCheck]:
    step_names = {
        str(step.get("name")) for step in payload.get("steps", []) if isinstance(step, dict)
    }
    skipped = {
        str(step.get("name"))
        for step in payload.get("steps", [])
        if isinstance(step, dict) and step.get("skipped")
    }
    return [
        _format_check("scheduled_backup", payload, "obelisk-scheduled-backup-report-v2"),
        _ok_check("scheduled_backup", payload),
        EvidenceCheck(
            "scheduled_backup:restore-drill",
            "restore_drill" in step_names and "restore_drill" not in skipped,
            "restore drill ran and was not skipped",
        ),
        EvidenceCheck(
            "scheduled_backup:audit-export",
            "audit_export" in step_names and "audit_export" not in skipped,
            "audit export ran and was not skipped",
        ),
        EvidenceCheck(
            "scheduled_backup:encrypted-artifact",
            "backup_encryption" in step_names
            and "backup_encryption" not in skipped
            and str(payload.get("backup_path", "")).endswith(".dump.enc")
            and isinstance(payload.get("backup_encryption"), dict)
            and payload["backup_encryption"].get("algorithm") == "AES-256-GCM",
            "backup encryption completed with an AES-256-GCM artifact",
        ),
    ]


def _verify_branch_protection(payload: dict[str, Any]) -> list[EvidenceCheck]:
    names = _check_names(payload)
    required = {
        "pull-request-required",
        "status-checks-required",
        "strict-status-checks",
        "admins-enforced",
    }
    missing = sorted(required - names)
    return [
        EvidenceCheck(
            "branch_protection:passed",
            payload.get("passed") is True,
            "branch protection verifier passed",
        ),
        EvidenceCheck(
            "branch_protection:required-checks",
            not missing,
            "required checks present" if not missing else "missing: " + ", ".join(missing),
        ),
    ]


def _verify_audit_retention(payload: dict[str, Any]) -> list[EvidenceCheck]:
    return [
        _format_check("audit_retention", payload, "obelisk-audit-retention-v1"),
        _ok_check("audit_retention", payload),
        EvidenceCheck(
            "audit_retention:verified-export",
            payload.get("verified_export") is True,
            "pre-prune audit export verified",
        ),
        EvidenceCheck(
            "audit_retention:signed-export",
            payload.get("signed_export") is True,
            "pre-prune audit export was signed",
        ),
    ]


def _verify_deployment_preflight(payload: dict[str, Any]) -> list[EvidenceCheck]:
    names = _check_names(payload)
    required = {
        "public-url-https",
        "public-health",
        "public-security-headers",
        "backend-not-public",
    }
    missing = sorted(required - names)
    return [
        _format_check("deployment_preflight", payload, "obelisk-deployment-preflight-v1"),
        _ok_check("deployment_preflight", payload),
        EvidenceCheck(
            "deployment_preflight:required-checks",
            not missing,
            "required checks present" if not missing else "missing: " + ", ".join(missing),
        ),
        EvidenceCheck(
            "deployment_preflight:backend-not-public",
            payload.get("backend_probe_performed") is True
            and payload.get("backend_publicly_reachable") is False,
            "direct backend exposure probe passed",
        ),
    ]


def _verify_secret_files(payload: dict[str, Any]) -> list[EvidenceCheck]:
    names = _check_names(payload)
    declared = {
        secret_name
        for secret_name in payload.get("required_secrets", [])
        if isinstance(secret_name, str)
    }
    required_suffixes = {
        "raw-empty",
        "file-configured",
        "file-readable",
        "file-prefix",
    }
    missing_by_secret: list[str] = []
    for secret_name in sorted(declared | {"UAM_BACKUP_ENCRYPTION_KEY"}):
        suffixes = {
            name.split(":", 1)[1]
            for name in names
            if name.startswith(f"{secret_name}:") and ":" in name
        }
        missing = sorted(required_suffixes - suffixes)
        if missing:
            missing_by_secret.append(f"{secret_name} missing {','.join(missing)}")
    return [
        _format_check("secret_files", payload, "obelisk-secret-files-preflight-v1"),
        _ok_check("secret_files", payload),
        EvidenceCheck(
            "secret_files:all-required-secrets-checked",
            not missing_by_secret and bool(declared),
            (
                "all required secrets have raw/file/read/prefix checks"
                if not missing_by_secret and bool(declared)
                else "; ".join(missing_by_secret) or "required_secrets missing"
            ),
        ),
    ]


def _verify_vault_import(payload: dict[str, Any]) -> list[EvidenceCheck]:
    return [
        _format_check("vault_import", payload, "obelisk-vault-import-report-v1"),
        _ok_check("vault_import", payload),
        EvidenceCheck(
            "vault_import:require-signature",
            payload.get("require_signature") is True,
            "vault import required a signed manifest",
        ),
        EvidenceCheck(
            "vault_import:verified-signed-manifest",
            payload.get("manifest_verified") is True and payload.get("manifest_signed") is True,
            "signed vault manifest verified before import planning/apply",
        ),
    ]


def _verify_ui_walkthrough(payload: dict[str, Any]) -> list[EvidenceCheck]:
    names = _check_names(payload)
    required = {
        "build-identity",
        "ui-served",
        "retain-recall",
        "conflict-decision",
        "vault-editable-text",
        "vault-archive",
        "model-settings-probe",
        "reindex",
        "metrics-surface",
    }
    skipped_model_probe = any(
        isinstance(item, dict)
        and item.get("name") == "model-settings-probe"
        and "skipped" in str(item.get("detail", "")).lower()
        for item in payload.get("checks", [])
    )
    missing = sorted(required - names)
    return [
        _format_check("ui_walkthrough", payload, "obelisk-ui-walkthrough-v1"),
        _ok_check("ui_walkthrough", payload),
        EvidenceCheck(
            "ui_walkthrough:required-checks",
            not missing,
            "required checks present" if not missing else "missing: " + ", ".join(missing),
        ),
        EvidenceCheck(
            "ui_walkthrough:model-probe-not-skipped",
            not skipped_model_probe,
            (
                "model settings probe ran"
                if not skipped_model_probe
                else "model settings probe was skipped"
            ),
        ),
    ]


def _format_check(name: str, payload: dict[str, Any], expected: str) -> EvidenceCheck:
    actual = payload.get("format")
    return EvidenceCheck(
        f"{name}:format",
        actual == expected,
        f"format={expected}" if actual == expected else f"expected {expected}, got {actual!r}",
    )


def _ok_check(name: str, payload: dict[str, Any]) -> EvidenceCheck:
    return EvidenceCheck(
        f"{name}:ok",
        payload.get("ok") is True,
        "ok=true" if payload.get("ok") is True else f"ok is {payload.get('ok')!r}",
    )


def _check_names(payload: dict[str, Any]) -> set[str]:
    checks = payload.get("checks", [])
    if not isinstance(checks, list):
        return set()
    return {str(item.get("name")) for item in checks if isinstance(item, dict) and item.get("name")}


if __name__ == "__main__":
    raise SystemExit(main())
