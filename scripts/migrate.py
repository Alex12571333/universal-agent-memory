"""Small forward-only migration runner for the standalone Docker server."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    ROOT / "migrations/001_initial.sql",
    ROOT / "migrations/dev/002_app_role.sql",
    ROOT / "migrations/003_outbox_delivery.sql",
    ROOT / "migrations/004_conflict_reviews.sql",
    ROOT / "migrations/005_memory_status.sql",
    ROOT / "migrations/006_conversation_ledger.sql",
    ROOT / "migrations/007_memory_proposals.sql",
    ROOT / "migrations/008_audit_events.sql",
    ROOT / "migrations/009_api_key_registry.sql",
)


def migrate(dsn: str) -> tuple[str, ...]:
    """Apply each SQL file once and baseline databases from pre-runner releases."""
    applied_now: list[str] = []
    with psycopg.connect(dsn) as connection:
        connection.execute("select pg_advisory_xact_lock(hashtext('uam-schema-migrations'))")
        connection.execute(
            """
            create table if not exists schema_migrations (
              name text primary key,
              applied_at timestamptz not null default now()
            )
            """
        )
        applied = {
            row[0]
            for row in connection.execute("select name from schema_migrations").fetchall()
        }
        if not applied:
            existing_schema = connection.execute(
                "select to_regclass('memory_items') is not null"
            ).fetchone()[0]
            existing_app_role = connection.execute(
                "select to_regrole('memory_app') is not null"
            ).fetchone()[0]
            if existing_schema:
                _record(connection, MIGRATIONS[0].name)
                applied.add(MIGRATIONS[0].name)
            if existing_app_role:
                _record(connection, MIGRATIONS[1].name)
                applied.add(MIGRATIONS[1].name)

        for path in MIGRATIONS:
            if path.name in applied:
                continue
            connection.execute(path.read_text())
            _record(connection, path.name)
            applied_now.append(path.name)
    return tuple(applied_now)


def _record(connection: psycopg.Connection, name: str) -> None:
    connection.execute(
        "insert into schema_migrations (name) values (%s) on conflict do nothing",
        (name,),
    )


if __name__ == "__main__":
    for migration in migrate(os.environ["UAM_ADMIN_DATABASE_URL"]):
        print(f"applied {migration}", flush=True)
