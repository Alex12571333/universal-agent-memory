"""Plan or apply safe Obsidian-style Markdown vault imports."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from memory_plane.api.app import DEFAULT_PROJECT_ID, DEFAULT_SERVER_ID
from memory_plane.bootstrap import build_postgres_container
from memory_plane.config.database import read_database_dsn
from memory_plane.config.secrets import read_secret_env
from memory_plane.services.vault import VaultImportSource

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_manifest import MANIFEST_NAME, verify_vault_manifest  # noqa: E402

REPORT_FORMAT = "obelisk-vault-import-report-v1"


@dataclass(frozen=True, slots=True)
class VaultImportReport:
    """Machine-readable evidence for release/operator vault import gates."""

    format: str
    ok: bool
    generated_at: str
    mode: str
    require_manifest: bool
    require_signature: bool
    manifest_verified: bool
    manifest_signed: bool
    manifest_file_count: int
    change_count: int
    supersede_count: int
    actions: dict[str, int]


def main() -> int:
    """Import edited Markdown files by creating superseding memory revisions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", help="Directory containing Markdown vault files")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply supersede writes. Omit for dry-run planning.",
    )
    parser.add_argument(
        "--database-url",
        default=read_database_dsn(),
        help="PostgreSQL app-role URL; defaults to UAM_DATABASE_URL",
    )
    parser.add_argument(
        "--tenant-id",
        type=UUID,
        default=UUID(os.getenv("UAM_SERVER_ID", str(DEFAULT_SERVER_ID))),
        help="Tenant/server UUID to import",
    )
    parser.add_argument(
        "--workspace-id",
        type=UUID,
        default=UUID(os.getenv("UAM_PROJECT_ID", str(DEFAULT_PROJECT_ID))),
        help="Workspace/project UUID to import",
    )
    parser.add_argument(
        "--require-manifest",
        action="store_true",
        help="Fail unless .uam-vault-manifest.json and checksum verify.",
    )
    parser.add_argument(
        "--require-signature",
        action="store_true",
        help="Fail unless .uam-vault-manifest.sig verifies with the signing key.",
    )
    parser.add_argument(
        "--signing-key",
        default=read_secret_env("UAM_VAULT_SIGNING_KEY"),
        help="HMAC key used to verify signed vault manifests.",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        help="Write obelisk-vault-import-report-v1 release evidence.",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("database URL is required")

    root = Path(args.input_dir)
    manifest_path = root / MANIFEST_NAME
    manifest_verified = False
    manifest_signed = False
    manifest_file_count = 0
    if args.require_manifest or args.require_signature or manifest_path.exists():
        verification = verify_vault_manifest(
            root,
            signing_key=args.signing_key,
            require_signature=args.require_signature,
        )
        manifest_verified = True
        manifest_signed = verification.signed
        manifest_file_count = verification.file_count
        signed = "signed" if verification.signed else "unsigned"
        print(f"verified {signed} vault manifest for {verification.file_count} markdown files")
    files = tuple(
        VaultImportSource(
            path=str(path.relative_to(root)),
            content=path.read_text(encoding="utf-8"),
        )
        for path in sorted(root.rglob("*.md"))
    )
    container = build_postgres_container(
        args.database_url,
        server_id=args.tenant_id,
        project_id=args.workspace_id,
    )
    result = (
        container.vault.apply_import(args.tenant_id, args.workspace_id, files)
        if args.apply
        else container.vault.plan_import(args.tenant_id, args.workspace_id, files)
    )
    mode = "applied" if args.apply else "planned"
    print(f"{mode} {len(result.changes)} vault files; supersede={result.supersede_count}")
    for change in result.changes:
        suffix = f" -> {change.new_item_id}" if change.new_item_id else ""
        print(f"{change.action}\t{change.path}\t{change.message}{suffix}")
    if args.json_report:
        report = _build_report(
            mode=mode,
            require_manifest=args.require_manifest,
            require_signature=args.require_signature,
            manifest_verified=manifest_verified,
            manifest_signed=manifest_signed,
            manifest_file_count=manifest_file_count,
            changes=result.changes,
            supersede_count=result.supersede_count,
        )
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


def _build_report(
    *,
    mode: str,
    require_manifest: bool,
    require_signature: bool,
    manifest_verified: bool,
    manifest_signed: bool,
    manifest_file_count: int,
    changes: Any,
    supersede_count: int,
) -> VaultImportReport:
    action_counts: dict[str, int] = {}
    change_count = 0
    for change in changes:
        change_count += 1
        action = str(getattr(change, "action", "unknown"))
        action_counts[action] = action_counts.get(action, 0) + 1
    return VaultImportReport(
        format=REPORT_FORMAT,
        ok=True,
        generated_at=datetime.now(UTC).isoformat(),
        mode=mode,
        require_manifest=require_manifest,
        require_signature=require_signature,
        manifest_verified=manifest_verified,
        manifest_signed=manifest_signed,
        manifest_file_count=manifest_file_count,
        change_count=change_count,
        supersede_count=supersede_count,
        actions=action_counts,
    )


if __name__ == "__main__":
    raise SystemExit(main())
