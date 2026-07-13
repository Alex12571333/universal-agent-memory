"""Restart-safe, scoped backfill for the optional protected lexical index.

The job runs with the restricted application role and requires one tenant and
workspace per invocation. It decrypts canonical text only inside PostgreSQL's
authorized application transaction, writes HMAC digests, and saves no memory
text in its state or report files.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from memory_plane.adapters.postgres import _PGCRYPTO_TEXT_PREFIX, PostgresMemoryLedger
from memory_plane.config.database import read_database_dsn
from memory_plane.services.protected_search import protected_index_digests


@dataclass(frozen=True, slots=True)
class BackfillCursor:
    """A durable lexicographic cursor, intentionally containing no memory text."""

    created_at: str | None = None
    memory_item_id: str | None = None


def _load_cursor(path: Path) -> BackfillCursor:
    if not path.exists():
        return BackfillCursor()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("backfill state must be a JSON object")
    created_at = value.get("created_at")
    memory_item_id = value.get("memory_item_id")
    if (created_at is None) != (memory_item_id is None):
        raise ValueError("backfill state must contain both cursor fields or neither")
    if created_at is not None:
        datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        UUID(str(memory_item_id))
    return BackfillCursor(
        created_at=None if created_at is None else str(created_at),
        memory_item_id=None if memory_item_id is None else str(memory_item_id),
    )


def _save_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically write operator state/report data with restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def backfill_workspace(
    ledger: PostgresMemoryLedger,
    *,
    tenant_id: UUID,
    workspace_id: UUID,
    cursor: BackfillCursor | None = None,
    batch_size: int = 500,
    max_batches: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Backfill one RLS-scoped workspace in committed, idempotent batches."""
    if ledger._protected_search_index_mode != "hmac-v1":
        raise ValueError("UAM_PROTECTED_SEARCH_INDEX must be hmac-v1 for backfill")
    if not 1 <= batch_size <= 10_000:
        raise ValueError("batch_size must be between 1 and 10000")
    if max_batches is not None and max_batches < 1:
        raise ValueError("max_batches must be positive when provided")

    active_cursor = cursor or BackfillCursor()
    rows_scanned = 0
    digests_written = 0
    batches = 0
    complete = False
    while max_batches is None or batches < max_batches:
        with ledger._connection() as connection:
            ledger._set_tenant(connection, tenant_id)
            rows = connection.execute(
                f"""
                select m.id, m.created_at,
                  case
                    when left(m.text, {len(_PGCRYPTO_TEXT_PREFIX)}) = '{_PGCRYPTO_TEXT_PREFIX}'
                    then pgp_sym_decrypt(
                      decode(substr(m.text, {len(_PGCRYPTO_TEXT_PREFIX) + 1}), 'base64'),
                      nullif(current_setting('app.memory_text_encryption_key', true), '')
                    )
                    else m.text
                  end as text
                from memory_items m
                where m.workspace_id = %s
                  and m.deleted_at is null
                  and (
                    %s::timestamptz is null
                    or (m.created_at, m.id) > (%s::timestamptz, %s::uuid)
                  )
                order by m.created_at, m.id
                limit %s
                """,
                (
                    workspace_id,
                    active_cursor.created_at,
                    active_cursor.created_at,
                    active_cursor.memory_item_id,
                    batch_size,
                ),
            ).fetchall()
            if not rows:
                complete = True
                break
            for row in rows:
                digests = protected_index_digests(row["text"], ledger._protected_search_index_key)
                if not dry_run:
                    for digest in digests:
                        connection.execute(
                            """
                            insert into memory_search_tokens (
                              tenant_id, workspace_id, memory_item_id, key_version, digest
                            ) values (%s, %s, %s, %s, %s)
                            on conflict do nothing
                            """,
                            (
                                tenant_id,
                                workspace_id,
                                row["id"],
                                ledger._protected_search_index_key_version,
                                digest,
                            ),
                        )
                rows_scanned += 1
                digests_written += len(digests)
                active_cursor = BackfillCursor(
                    created_at=row["created_at"].astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    memory_item_id=str(row["id"]),
                )
        batches += 1

    return {
        "format": "obelisk-protected-search-backfill-v1",
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "tenant_id": str(tenant_id),
        "workspace_id": str(workspace_id),
        "key_version": ledger._protected_search_index_key_version,
        "dry_run": dry_run,
        "complete": complete,
        "batches": batches,
        "rows_scanned": rows_scanned,
        "digests_written": digests_written,
        "cursor": asdict(active_cursor),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--workspace-id", required=True, type=UUID)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cursor = _load_cursor(args.state_file)
    ledger = PostgresMemoryLedger(read_database_dsn())
    report = backfill_workspace(
        ledger,
        tenant_id=args.tenant_id,
        workspace_id=args.workspace_id,
        cursor=cursor,
        batch_size=args.batch_size,
        max_batches=args.max_batches,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        _save_json(args.state_file, report["cursor"])
    if args.report:
        _save_json(args.report, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
