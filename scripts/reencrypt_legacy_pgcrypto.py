"""Incrementally encrypt legacy Obelisk PostgreSQL fields with pgcrypto.

Use an administrator DSN, never the restricted runtime application role. The
operation is restart-safe: every batch selects only plaintext rows, commits,
and can be run again until the report contains zero pending rows.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from memory_plane.config.database import read_database_dsn
from memory_plane.config.secrets import read_secret_env

PREFIX = "enc:pgcrypto:v1:"
JSON_KEY = "_uam_encrypted_json_v1"


@dataclass(frozen=True, slots=True)
class ReencryptionStep:
    """One bounded plaintext-to-pgcrypto conversion."""

    name: str
    pending_where: str
    update_sql: str


def _text_step(name: str, table: str, column: str) -> ReencryptionStep:
    pending = f"{column} is not null and left({column}, {len(PREFIX)}) <> '{PREFIX}'"
    return ReencryptionStep(
        name=name,
        pending_where=pending,
        update_sql=f"""
        with batch as (
          select ctid from {table}
          where {pending}
          order by ctid
          limit %s
          for update skip locked
        )
        update {table} target
        set {column} = %s || encode(
          pgp_sym_encrypt(target.{column}, %s, 'cipher-algo=aes256,compress-algo=0'),
          'base64'
        )
        from batch
        where target.ctid = batch.ctid
        """,
    )


def _json_step(name: str, table: str, column: str) -> ReencryptionStep:
    pending = (
        f"not ({column} ? '{JSON_KEY}' and ({column} - '{JSON_KEY}') = '{{}}'::jsonb "
        f"and left({column} ->> '{JSON_KEY}', {len(PREFIX)}) = '{PREFIX}')"
    )
    return ReencryptionStep(
        name=name,
        pending_where=pending,
        update_sql=f"""
        with batch as (
          select ctid from {table}
          where {pending}
          order by ctid
          limit %s
          for update skip locked
        )
        update {table} target
        set {column} = jsonb_build_object(
          %s,
          %s || encode(
            pgp_sym_encrypt(target.{column}::text, %s, 'cipher-algo=aes256,compress-algo=0'),
            'base64'
          )
        )
        from batch
        where target.ctid = batch.ctid
        """,
    )


STEPS = (
    _text_step("memory_items.text", "memory_items", "text"),
    _text_step("memory_provenance.quote_text", "memory_provenance", "quote_text"),
    _text_step("conversation_messages.content", "conversation_messages", "content"),
    _text_step("memory_proposals.proposal", "memory_proposals", "proposal"),
    _text_step("memory_proposals.evidence", "memory_proposals", "evidence"),
    _text_step("observations.summary", "observations", "summary"),
    _json_step("audit_events.metadata", "audit_events", "metadata"),
    _json_step("checkpoints.state", "checkpoints", "state"),
)


def _count_pending(connection: psycopg.Connection[Any], step: ReencryptionStep) -> int:
    row = connection.execute(
        f"select count(*) as total from {step.name.rsplit('.', 1)[0]} where {step.pending_where}"
    ).fetchone()
    return int(row["total"] if isinstance(row, dict) else row[0])


def reencrypt_legacy(
    dsn: str,
    key: str,
    *,
    batch_size: int = 500,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Encrypt legacy rows in committed batches and return a non-secret report."""
    if not key.strip():
        raise ValueError("UAM_MEMORY_TEXT_ENCRYPTION_KEY(_FILE) is required")
    if not 1 <= batch_size <= 10_000:
        raise ValueError("batch_size must be between 1 and 10000")

    steps: list[dict[str, Any]] = []
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as connection:
        for step in STEPS:
            pending_before = _count_pending(connection, step)
            migrated = 0
            if not dry_run:
                while True:
                    result = connection.execute(
                        step.update_sql,
                        (batch_size, JSON_KEY, PREFIX, key)
                        if "jsonb_build_object" in step.update_sql
                        else (batch_size, PREFIX, key),
                    )
                    changed = result.rowcount
                    connection.commit()
                    migrated += changed
                    if changed < batch_size:
                        break
            pending_after = _count_pending(connection, step)
            steps.append(
                {
                    "name": step.name,
                    "pending_before": pending_before,
                    "migrated": migrated,
                    "pending_after": pending_after,
                }
            )
    complete = all(step["pending_after"] == 0 for step in steps)
    return {
        "format": "obelisk-pgcrypto-legacy-reencryption-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
        "ok": complete if not dry_run else True,
        "complete": complete,
        "steps": steps,
    }


def parse_args() -> argparse.Namespace:
    """Parse an explicit bounded migration request."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def main() -> None:
    """Run the administrator-only migration and write an optional JSON report."""
    args = parse_args()
    dsn = read_database_dsn("UAM_ADMIN_DATABASE_URL", component_prefix="UAM_ADMIN_DATABASE")
    if not dsn:
        raise SystemExit("administrator database connection is required")
    key = read_secret_env("UAM_MEMORY_TEXT_ENCRYPTION_KEY")
    report = reencrypt_legacy(dsn, key or "", batch_size=args.batch_size, dry_run=args.dry_run)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
