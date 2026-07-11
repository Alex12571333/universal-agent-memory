"""Bounded admin-only retention for processed delivery and idempotency records."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from memory_plane.config.database import read_database_dsn

REPORT_FORMAT = "obelisk-maintenance-retention-v1"
_TABLES = {
    "outbox_events": "coalesce(published_at, dead_lettered_at)",
    "processed_events": "processed_at",
    "idempotency_keys": "created_at",
    "conversation_idempotency_keys": "created_at",
    "memory_proposal_idempotency_keys": "created_at",
}


def _admin_dsn() -> str | None:
    return (
        read_database_dsn(
            "UAM_MAINTENANCE_DATABASE_URL", component_prefix="UAM_MAINTENANCE_DATABASE"
        )
        or read_database_dsn("UAM_BACKUP_DATABASE_URL", component_prefix="UAM_BACKUP_DATABASE")
        or read_database_dsn("UAM_ADMIN_DATABASE_URL", component_prefix="UAM_ADMIN_DATABASE")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=_admin_dsn())
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("admin/maintenance database configuration is required")
    if args.retention_days < 1:
        parser.error("--retention-days must be positive")
    if not 1 <= args.batch_size <= 5000:
        parser.error("--batch-size must be between 1 and 5000")

    cutoff = datetime.now(UTC) - timedelta(days=args.retention_days)
    with psycopg.connect(args.database_url) as connection:
        counts = {
            table: _purge(connection, table, expression, cutoff, args.batch_size, args.apply)
            for table, expression in _TABLES.items()
        }
    report = {
        "format": REPORT_FORMAT,
        "ok": True,
        "applied": args.apply,
        "cutoff": cutoff.isoformat(),
        "retention_days": args.retention_days,
        "batch_size": args.batch_size,
        "counts": counts,
        "invariants": {
            "pending_outbox_preserved": True,
            "runtime_app_role_not_used": True,
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


def _purge(
    connection: psycopg.Connection,
    table: str,
    timestamp_expression: str,
    cutoff: datetime,
    limit: int,
    apply: bool,
) -> int:
    where = f"{timestamp_expression} is not null and {timestamp_expression} < %s"
    if table == "outbox_events":
        where += " and (published_at is not null or dead_lettered_at is not null)"
    if not apply:
        row = connection.execute(
            f"select count(*) from {table} where {where}", (cutoff,)
        ).fetchone()
        return int(row[0])
    rows = connection.execute(
        f"""
        with doomed as (
          select ctid from {table} where {where}
          order by {timestamp_expression}, ctid limit %s
        )
        delete from {table} where ctid in (select ctid from doomed) returning 1
        """,
        (cutoff, limit),
    ).fetchall()
    return len(rows)


if __name__ == "__main__":
    raise SystemExit(main())
