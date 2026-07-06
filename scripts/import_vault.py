"""Plan or apply safe Obsidian-style Markdown vault imports."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from uuid import UUID

from memory_plane.api.app import DEFAULT_PROJECT_ID, DEFAULT_SERVER_ID
from memory_plane.bootstrap import build_postgres_container
from memory_plane.services.vault import VaultImportSource


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
        default=os.getenv("UAM_DATABASE_URL"),
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
    args = parser.parse_args()
    if not args.database_url:
        parser.error("database URL is required")

    root = Path(args.input_dir)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
