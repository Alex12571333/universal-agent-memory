from __future__ import annotations

import pytest

from memory_plane.adapters.postgres import PostgresMemoryLedger


class _AclResult:
    def __init__(self, row: dict[str, bool]) -> None:
        self._row = row

    def fetchone(self) -> dict[str, bool]:
        return self._row


class _AclConnection:
    def __init__(self, row: dict[str, bool]) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, params: tuple[object, ...]) -> _AclResult:
        self.calls.append((query, params))
        return _AclResult(self.row)


def _safe_row() -> dict[str, bool]:
    return {
        "outbox_events_update": True,
        "checkpoints_delete": True,
        "memory_items_update": False,
        "memory_items_delete": False,
        "audit_events_update": False,
        "audit_events_delete": False,
    }


def test_runtime_acl_verification_allows_only_required_mutations() -> None:
    connection = _AclConnection(_safe_row())

    PostgresMemoryLedger._verify_runtime_acl(connection)

    assert connection.calls
    query, params = connection.calls[0]
    assert "has_table_privilege(current_user" in query
    assert params == (
        "outbox_events",
        "update",
        "checkpoints",
        "delete",
        "memory_items",
        "update",
        "memory_items",
        "delete",
        "audit_events",
        "update",
        "audit_events",
        "delete",
    )


@pytest.mark.parametrize("unsafe", ["memory_items_update", "audit_events_delete"])
def test_runtime_acl_verification_rejects_broad_canonical_or_audit_mutation(
    unsafe: str,
) -> None:
    row = _safe_row()
    row[unsafe] = True

    with pytest.raises(RuntimeError, match=f"forbidden:{unsafe}"):
        PostgresMemoryLedger._verify_runtime_acl(_AclConnection(row))


def test_runtime_acl_verification_rejects_missing_operational_privilege() -> None:
    row = _safe_row()
    row["outbox_events_update"] = False

    with pytest.raises(RuntimeError, match="missing:outbox_events_update"):
        PostgresMemoryLedger._verify_runtime_acl(_AclConnection(row))
