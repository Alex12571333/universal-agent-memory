from __future__ import annotations

from uuid import uuid4

import pytest

from memory_plane.adapters.postgres import PostgresMemoryLedger
from memory_plane.domain.models import MemoryItem, MemoryLayer, MemoryScope, Provenance


class _FakeRowConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...]) -> object:
        self.calls.append((sql, params))
        return self

    def fetchone(self) -> dict[str, str]:
        return {"encrypted_text": "enc:pgcrypto:v1:ciphertext"}


def test_postgres_pgcrypto_mode_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION", "pgcrypto")
    monkeypatch.delenv("UAM_MEMORY_TEXT_ENCRYPTION_KEY", raising=False)

    with pytest.raises(ValueError, match="UAM_MEMORY_TEXT_ENCRYPTION_KEY"):
        PostgresMemoryLedger("postgresql://example/memory")


def test_postgres_encrypts_memory_text_before_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION", "pgcrypto")
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION_KEY", "memtext_" + "a" * 40)
    ledger = PostgresMemoryLedger("postgresql://example/memory")
    connection = _FakeRowConnection()
    item = MemoryItem(
        tenant_id=uuid4(),
        workspace_id=uuid4(),
        layer=MemoryLayer.SEMANTIC,
        scope=MemoryScope.WORKSPACE,
        kind="fact",
        text="sensitive canonical memory",
        provenance=Provenance(source_kind="test"),
    )

    stored = ledger._stored_memory_text(connection, item.text)

    assert stored == "enc:pgcrypto:v1:ciphertext"
    assert connection.calls
    sql, params = connection.calls[0]
    assert "pgp_sym_encrypt" in sql
    assert params[0] == "enc:pgcrypto:v1:"
    assert params[1] == "sensitive canonical memory"
    assert params[2] == "memtext_" + "a" * 40

