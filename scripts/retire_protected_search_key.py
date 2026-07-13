"""Retire one old protected-search key version after verified replacement coverage."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from memory_plane.adapters.postgres import PostgresMemoryLedger
from memory_plane.config.database import read_database_dsn
from memory_plane.services.protected_search import protected_document_marker


def _admin_dsn() -> str | None:
    """Use only an explicitly configured administrator DSN for destructive apply."""
    return read_database_dsn("UAM_ADMIN_DATABASE_URL", component_prefix="UAM_ADMIN_DATABASE")


def retire_key_version(
    dsn: str,
    ledger: PostgresMemoryLedger,
    *,
    tenant_id: UUID,
    workspace_id: UUID,
    retire_key_version: int,
    apply: bool = False,
) -> dict[str, Any]:
    """Delete old digests only if every non-deleted row has the active marker."""
    active_version = ledger._protected_search_index_key_version
    if ledger._protected_search_index_mode != "hmac-v1":
        raise ValueError("UAM_PROTECTED_SEARCH_INDEX must be hmac-v1")
    if not 0 < retire_key_version <= 32767 or retire_key_version == active_version:
        raise ValueError("retire key version must differ from the active positive key version")
    marker = protected_document_marker(ledger._protected_search_index_key)
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        can_delete = bool(
            connection.execute(
                "select has_table_privilege("
                "current_user, 'memory_search_tokens', 'delete') as allowed"
            ).fetchone()["allowed"]
        )
        coverage = bool(
            connection.execute(
                """
                select not exists (
                  select 1 from memory_items m
                  where m.tenant_id = %s
                    and m.workspace_id = %s
                    and m.deleted_at is null
                    and not exists (
                      select 1 from memory_search_tokens t
                      where t.tenant_id = m.tenant_id
                        and t.workspace_id = m.workspace_id
                        and t.memory_item_id = m.id
                        and t.key_version = %s
                        and t.digest = %s
                    )
                ) as complete
                """,
                (tenant_id, workspace_id, active_version, marker),
            ).fetchone()["complete"]
        )
        old_count = int(
            connection.execute(
                """
                select count(*) as count
                from memory_search_tokens
                where tenant_id = %s and workspace_id = %s and key_version = %s
                """,
                (tenant_id, workspace_id, retire_key_version),
            ).fetchone()["count"]
        )
        deleted = 0
        if apply:
            if not can_delete:
                raise PermissionError("--apply requires an administrator database role")
            if not coverage:
                raise RuntimeError("active protected-search key coverage is incomplete")
            deleted = int(
                connection.execute(
                    """
                    delete from memory_search_tokens
                    where tenant_id = %s and workspace_id = %s and key_version = %s
                    returning memory_item_id
                    """,
                    (tenant_id, workspace_id, retire_key_version),
                ).rowcount
            )
    return {
        "format": "obelisk-protected-search-key-retirement-v1",
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "dry_run": not apply,
        "tenant_id": str(tenant_id),
        "workspace_id": str(workspace_id),
        "active_key_version": active_version,
        "retired_key_version": retire_key_version,
        "active_coverage_complete": coverage,
        "old_digest_count": old_count,
        "deleted_digest_count": deleted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=UUID, required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--retire-key-version", type=int, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    dsn = _admin_dsn()
    if not dsn:
        parser.error("UAM_ADMIN_DATABASE_URL (or components) is required")
    report = retire_key_version(
        dsn,
        PostgresMemoryLedger(dsn),
        tenant_id=args.tenant_id,
        workspace_id=args.workspace_id,
        retire_key_version=args.retire_key_version,
        apply=args.apply,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
