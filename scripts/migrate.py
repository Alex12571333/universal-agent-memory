"""Small forward-only migration runner for the standalone Docker server."""

from __future__ import annotations

import os
import re
from pathlib import Path

import psycopg
from psycopg import sql

from memory_plane.config.database import read_database_dsn
from memory_plane.config.secrets import read_secret_env

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
    ROOT / "migrations/010_conflict_resolution_memory.sql",
    ROOT / "migrations/011_conversation_staging_retention.sql",
    ROOT / "migrations/012_outbox_retry_schedule.sql",
    ROOT / "migrations/013_protected_search_tokens.sql",
    ROOT / "migrations/014_protected_search_scope_integrity.sql",
)


_ROLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")


def migrate(
    dsn: str,
    *,
    app_user: str | None = None,
    app_password: str | None = None,
) -> tuple[str, ...]:
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
            if existing_schema:
                _record(connection, MIGRATIONS[0].name)
                applied.add(MIGRATIONS[0].name)

        for path in MIGRATIONS:
            if path.name in applied:
                continue
            connection.execute(path.read_text())
            _record(connection, path.name)
            applied_now.append(path.name)
        if app_user is not None or app_password is not None:
            if not app_user or not app_password:
                raise ValueError("both app_user and app_password are required")
            provision_application_role(connection, app_user, app_password)
    return tuple(applied_now)


def provision_application_role(
    connection: psycopg.Connection,
    username: str,
    password: str,
) -> None:
    """Create or rotate the least-privileged runtime login idempotently."""
    if not _ROLE_NAME.fullmatch(username):
        raise ValueError("application database role is not a valid PostgreSQL identifier")
    if username.lower() == "postgres" or username.lower().startswith("pg_"):
        raise ValueError("application database role uses a reserved PostgreSQL name")
    if not password:
        raise ValueError("application database password must not be empty")

    administrator = connection.execute("select current_user").fetchone()[0]
    if username == administrator:
        raise ValueError("application database role must differ from the administrator role")

    identifier = sql.Identifier(username)
    exists = connection.execute(
        "select exists(select 1 from pg_roles where rolname = %s)",
        (username,),
    ).fetchone()[0]
    role_options = sql.SQL(
        " login password %s nosuperuser nocreatedb nocreaterole noinherit noreplication"
    )
    if exists:
        statement = sql.SQL("alter role {} with").format(identifier) + role_options
    else:
        statement = sql.SQL("create role {}").format(identifier) + role_options
    # PostgreSQL utility statements do not accept server-side `$1` parameters.
    # ClientCursor performs psycopg's type-aware client-side quoting instead of
    # string interpolation, so arbitrary password characters remain safe.
    with psycopg.ClientCursor(connection) as cursor:
        cursor.execute(statement, (password,))

    connection.execute(sql.SQL("grant usage on schema public to {}").format(identifier))
    connection.execute(
        sql.SQL("grant select, insert on all tables in schema public to {}").format(identifier)
    )
    connection.execute(
        sql.SQL("revoke update, delete on all tables in schema public from {}").format(identifier)
    )
    connection.execute(
        sql.SQL(
            "grant update on outbox_events, processed_events, api_key_registry, "
            "conversation_turns, conversation_messages, memory_proposals, agents, "
            "threads, conflict_reviews to {}"
        ).format(identifier)
    )
    connection.execute(sql.SQL("grant delete on checkpoints to {}").format(identifier))
    connection.execute(
        sql.SQL("grant usage, select on all sequences in schema public to {}").format(
            identifier
        )
    )
    connection.execute(
        sql.SQL(
            "alter default privileges in schema public "
            "grant select, insert on tables to {}"
        ).format(identifier)
    )
    connection.execute(
        sql.SQL(
            "alter default privileges in schema public "
            "grant usage, select on sequences to {}"
        ).format(identifier)
    )


def _record(connection: psycopg.Connection, name: str) -> None:
    connection.execute(
        "insert into schema_migrations (name) values (%s) on conflict do nothing",
        (name,),
    )


if __name__ == "__main__":
    dsn = read_database_dsn(
        "UAM_ADMIN_DATABASE_URL",
        component_prefix="UAM_ADMIN_DATABASE",
    )
    if not dsn:
        raise SystemExit("administrator database connection is required")
    app_user = os.getenv("UAM_APP_DB_USER")
    app_password = read_secret_env("UAM_APP_DB_PASSWORD")
    if not app_user or not app_password:
        raise SystemExit("UAM_APP_DB_USER and UAM_APP_DB_PASSWORD(_FILE) are required")
    for migration in migrate(dsn, app_user=app_user, app_password=app_password):
        print(f"applied {migration}", flush=True)
