"""Capture non-secret PostgreSQL plan evidence for the protected search index."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from memory_plane.adapters.postgres import PostgresMemoryLedger
from memory_plane.config.database import read_database_dsn
from memory_plane.services.protected_search import protected_tokens

_DIGEST_LITERAL = re.compile(r"\\x[0-9a-fA-F]+")


def _redact_plan(value: Any) -> Any:
    """Keep planner shape while removing literal HMAC values from evidence."""
    if isinstance(value, dict):
        return {str(key): _redact_plan(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_plan(item) for item in value]
    if isinstance(value, str):
        return _DIGEST_LITERAL.sub(r"\\x[redacted-hmac]", value)
    return value


def _index_names(value: Any) -> tuple[str, ...]:
    """Extract used index names from PostgreSQL JSON EXPLAIN output."""
    names: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            index_name = item.get("Index Name")
            if isinstance(index_name, str):
                names.add(index_name)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return tuple(sorted(names))


def capture_probe(
    ledger: PostgresMemoryLedger,
    *,
    tenant_id: UUID,
    workspace_id: UUID,
    query: str,
) -> dict[str, Any]:
    """Prove the scoped digest lookup can use its intended B-tree index."""
    if ledger._protected_search_index_mode != "hmac-v1":
        raise ValueError("UAM_PROTECTED_SEARCH_INDEX must be hmac-v1")
    digests = list(protected_tokens(query, ledger._protected_search_index_key))
    if not digests:
        raise ValueError("query must contain at least one searchable token")
    coverage = ledger._protected_search_index_is_complete(
        SimpleNamespace(tenant_id=tenant_id, workspace_id=workspace_id, text=query)
    )
    with ledger._connection() as connection:
        ledger._set_tenant(connection, tenant_id)
        connection.execute("set local enable_seqscan = off")
        row = connection.execute(
            """
            explain (format json, costs true)
            select memory_item_id
            from memory_search_tokens
            where tenant_id = %s
              and workspace_id = %s
              and key_version = %s
              and digest = any(%s)
            """,
            (tenant_id, workspace_id, ledger._protected_search_index_key_version, digests),
        ).fetchone()
    plan = row["QUERY PLAN"]
    index_names = _index_names(plan)
    expected_index = "memory_search_tokens_lookup_idx"
    return {
        "format": "obelisk-protected-search-index-probe-v1",
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "tenant_id": str(tenant_id),
        "workspace_id": str(workspace_id),
        "key_version": ledger._protected_search_index_key_version,
        "query_token_count": len(digests),
        "coverage_complete": coverage,
        "expected_index": expected_index,
        "used_indexes": index_names,
        "index_used": expected_index in index_names,
        "plan": _redact_plan(plan),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=UUID, required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    report = capture_probe(
        PostgresMemoryLedger(read_database_dsn()),
        tenant_id=args.tenant_id,
        workspace_id=args.workspace_id,
        query=args.query,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not report["coverage_complete"] or not report["index_used"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
