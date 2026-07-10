"""Generate the full release-evidence manifest skeleton."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from verify_release_evidence import MANIFEST_FORMAT, REQUIRED_ARTIFACTS

DEFAULT_ARTIFACT_PATHS = {
    "agent_soak": "ops/agent-soak.json",
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
    """Write a complete release-evidence manifest skeleton."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, help="Release identifier.")
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

    artifacts = build_artifacts(tuple(args.artifact))
    manifest = {
        "format": MANIFEST_FORMAT,
        "release": args.release,
        "artifacts": artifacts,
    }
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


if __name__ == "__main__":
    raise SystemExit(main())
