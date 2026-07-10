"""Verify release evidence artifacts for a full-production Obelisk Memory release."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MANIFEST_FORMAT = "obelisk-release-evidence-manifest-v1"

REQUIRED_ARTIFACTS = {
    "agent_soak",
    "memory_llm",
    "load_smoke",
    "metrics_health",
    "ops_schedule",
    "observability",
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
        help="Path to obelisk-release-evidence-manifest-v1 JSON",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON result")
    args = parser.parse_args()

    checks = verify_manifest(args.manifest)
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


def verify_manifest(path: Path) -> list[EvidenceCheck]:
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

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        checks.append(EvidenceCheck("manifest:artifacts", False, "artifacts must be an object"))
        return checks
    missing = sorted(REQUIRED_ARTIFACTS - set(artifacts))
    checks.append(
        EvidenceCheck(
            "manifest:required-artifacts",
            not missing,
            (
                "all required artifacts listed"
                if not missing
                else "missing artifacts: " + ", ".join(missing)
            ),
        )
    )

    base = path.parent
    readers = {
        "agent_soak": _verify_agent_soak,
        "memory_llm": _verify_memory_llm,
        "load_smoke": _verify_load_smoke,
        "metrics_health": _verify_metrics_health,
        "ops_schedule": _verify_ops_schedule,
        "observability": _verify_observability,
        "scheduled_backup": _verify_scheduled_backup,
        "audit_retention": _verify_audit_retention,
        "deployment_preflight": _verify_deployment_preflight,
        "secret_files": _verify_secret_files,
        "vault_import": _verify_vault_import,
        "branch_protection": _verify_branch_protection,
        "ui_walkthrough": _verify_ui_walkthrough,
    }
    for name, verifier in readers.items():
        raw_path = artifacts.get(name)
        artifact_path = _artifact_path(base, raw_path)
        try:
            payload = _read_json(artifact_path)
        except Exception as exc:  # noqa: BLE001 - keep checking all artifacts.
            checks.append(
                EvidenceCheck(
                    f"{name}:read",
                    False,
                    f"{artifact_path}: {type(exc).__name__}: {exc}",
                )
            )
            continue
        checks.extend(verifier(payload))
    return checks


def _artifact_path(base: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("artifact path must be a non-empty string")
    path = Path(raw_path)
    return path if path.is_absolute() else base / path


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    return payload


def _verify_agent_soak(payload: dict[str, Any]) -> list[EvidenceCheck]:
    checks = [
        _format_check("agent_soak", payload, "obelisk-agent-soak-v1"),
        _ok_check("agent_soak", payload),
    ]
    names = _check_names(payload)
    required = {
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
            int(payload.get("agents") or 0) >= 2
            and int(payload.get("total_operations") or 0) >= 4,
            "parallel load evidence present",
        ),
    ]


def _verify_scheduled_backup(payload: dict[str, Any]) -> list[EvidenceCheck]:
    step_names = {
        str(step.get("name"))
        for step in payload.get("steps", [])
        if isinstance(step, dict)
    }
    skipped = {
        str(step.get("name"))
        for step in payload.get("steps", [])
        if isinstance(step, dict) and step.get("skipped")
    }
    return [
        _format_check("scheduled_backup", payload, "obelisk-scheduled-backup-report-v1"),
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
    required_suffixes = {
        "raw-empty",
        "file-configured",
        "file-readable",
        "file-prefix",
    }
    missing_by_secret: list[str] = []
    for secret_name in payload.get("required_secrets", []):
        if not isinstance(secret_name, str):
            continue
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
            not missing_by_secret and bool(payload.get("required_secrets")),
            (
                "all required secrets have raw/file/read/prefix checks"
                if not missing_by_secret and bool(payload.get("required_secrets"))
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
    return {
        str(item.get("name"))
        for item in checks
        if isinstance(item, dict) and item.get("name")
    }


if __name__ == "__main__":
    raise SystemExit(main())
