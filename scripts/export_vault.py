"""Materialize an Obsidian-style Markdown vault from the memory database."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from uuid import UUID

from memory_plane.api.app import DEFAULT_PROJECT_ID, DEFAULT_SERVER_ID
from memory_plane.bootstrap import build_postgres_container

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_manifest import write_vault_manifest  # noqa: E402


def main() -> int:
    """Export canonical memory into a local directory of Markdown files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", help="Directory where Markdown files are written")
    parser.add_argument(
        "--database-url",
        default=os.getenv("UAM_DATABASE_URL"),
        help="PostgreSQL app-role URL; defaults to UAM_DATABASE_URL",
    )
    parser.add_argument(
        "--tenant-id",
        type=UUID,
        default=UUID(os.getenv("UAM_SERVER_ID", str(DEFAULT_SERVER_ID))),
        help="Tenant/server UUID to export",
    )
    parser.add_argument(
        "--workspace-id",
        type=UUID,
        default=UUID(os.getenv("UAM_PROJECT_ID", str(DEFAULT_PROJECT_ID))),
        help="Workspace/project UUID to export",
    )
    parser.add_argument(
        "--no-manifest",
        action="store_true",
        help="Do not write .uam-vault-manifest.json/checksum files.",
    )
    parser.add_argument(
        "--signing-key",
        default=os.getenv("UAM_VAULT_SIGNING_KEY"),
        help="Optional HMAC key used to write .uam-vault-manifest.sig.",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("database URL is required")

    container = build_postgres_container(
        args.database_url,
        server_id=args.tenant_id,
        project_id=args.workspace_id,
    )
    export = container.vault.export(args.tenant_id, args.workspace_id)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for file in export.files:
        relative_path = Path(file.path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"unsafe vault path: {file.path}")
        target = output / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file.content, encoding="utf-8")
    if not args.no_manifest:
        manifest = write_vault_manifest(
            output,
            tenant_id=str(args.tenant_id),
            workspace_id=str(args.workspace_id),
            signing_key=args.signing_key,
        )
        signed = " signed" if args.signing_key else ""
        print(f"wrote{signed} vault manifest for {len(manifest['files'])} markdown files")
    print(f"exported {len(export.files)} files to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
