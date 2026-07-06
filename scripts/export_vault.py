"""Materialize an Obsidian-style Markdown vault from the memory database."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from uuid import UUID

from memory_plane.api.app import DEFAULT_PROJECT_ID, DEFAULT_SERVER_ID
from memory_plane.bootstrap import build_postgres_container


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
    print(f"exported {len(export.files)} files to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
