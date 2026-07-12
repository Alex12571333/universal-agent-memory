from __future__ import annotations

from uuid import uuid4

import pytest

from memory_plane.adapters.postgres import (
    _AUDIT_METADATA_SQL,
    _CHECKPOINT_STATE_SQL,
    _CONVERSATION_CONTENT_SQL,
    _OBSERVATION_SUMMARY_SQL,
    _PGCRYPTO_JSON_KEY,
    _PROPOSAL_EVIDENCE_SQL,
    _PROPOSAL_TEXT_SQL,
    _PROVENANCE_QUOTE_SQL,
    PostgresMemoryLedger,
)
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

    stored = ledger._stored_memory_text(connection, item)

    assert stored == "enc:pgcrypto:v1:ciphertext"
    assert connection.calls
    sql, params = connection.calls[0]
    assert "pgp_sym_encrypt" in sql
    assert params[0] == "enc:pgcrypto:v1:"
    assert params[1] == "sensitive canonical memory"
    assert params[2] == "memtext_" + "a" * 40


def test_postgres_encrypts_only_selected_memory_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION", "pgcrypto")
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION_KEY", "memtext_" + "a" * 40)
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION_SCOPES", "private,thread")
    ledger = PostgresMemoryLedger("postgresql://example/memory")
    connection = _FakeRowConnection()
    workspace_item = MemoryItem(
        tenant_id=uuid4(),
        workspace_id=uuid4(),
        layer=MemoryLayer.SEMANTIC,
        scope=MemoryScope.WORKSPACE,
        kind="fact",
        text="workspace text can remain plaintext by policy",
        provenance=Provenance(source_kind="test"),
    )
    thread_item = MemoryItem(
        tenant_id=workspace_item.tenant_id,
        workspace_id=workspace_item.workspace_id,
        thread_id=uuid4(),
        layer=MemoryLayer.EPISODIC,
        scope=MemoryScope.THREAD,
        kind="turn_summary",
        text="thread text must be encrypted",
        provenance=Provenance(source_kind="test"),
    )

    plaintext = ledger._stored_memory_text(connection, workspace_item)
    ciphertext = ledger._stored_memory_text(connection, thread_item)

    assert plaintext == "workspace text can remain plaintext by policy"
    assert ciphertext == "enc:pgcrypto:v1:ciphertext"
    assert len(connection.calls) == 1


def test_postgres_rejects_unknown_memory_text_encryption_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION", "pgcrypto")
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION_KEY", "memtext_" + "a" * 40)
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION_SCOPES", "private,nope")

    with pytest.raises(ValueError, match="UAM_MEMORY_TEXT_ENCRYPTION_SCOPES"):
        PostgresMemoryLedger("postgresql://example/memory")


def test_postgres_reads_memory_text_encryption_key_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    secret_file = tmp_path / "memory-text-key"
    secret_file.write_text("memtext_" + "b" * 40 + "\n", encoding="utf-8")
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION", "pgcrypto")
    monkeypatch.delenv("UAM_MEMORY_TEXT_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION_KEY_FILE", str(secret_file))

    ledger = PostgresMemoryLedger("postgresql://example/memory")

    assert ledger._text_encryption_key == "memtext_" + "b" * 40


def test_postgres_encrypts_and_decrypts_raw_conversation_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION", "pgcrypto")
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION_KEY", "memtext_" + "a" * 40)
    ledger = PostgresMemoryLedger("postgresql://example/memory")
    connection = _FakeRowConnection()

    ciphertext = ledger._stored_sensitive_text(connection, "raw agent conversation")

    assert ciphertext == "enc:pgcrypto:v1:ciphertext"
    assert connection.calls[0][1][1] == "raw agent conversation"
    assert "pgp_sym_decrypt" in _CONVERSATION_CONTENT_SQL
    assert "m.content" in _CONVERSATION_CONTENT_SQL


def test_postgres_decrypt_queries_cover_proposal_and_evidence_columns() -> None:
    assert "p.proposal" in _PROPOSAL_TEXT_SQL
    assert "p.evidence" in _PROPOSAL_EVIDENCE_SQL
    assert "pgp_sym_decrypt" in _PROPOSAL_TEXT_SQL
    assert "pgp_sym_decrypt" in _PROPOSAL_EVIDENCE_SQL


def test_postgres_pgcrypto_wraps_noncanonical_json_and_reads_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION", "pgcrypto")
    monkeypatch.setenv("UAM_MEMORY_TEXT_ENCRYPTION_KEY", "memtext_" + "a" * 40)
    ledger = PostgresMemoryLedger("postgresql://example/memory")
    connection = _FakeRowConnection()

    stored = ledger._stored_sensitive_json(connection, {"query": "private detail"})

    assert stored == {_PGCRYPTO_JSON_KEY: "enc:pgcrypto:v1:ciphertext"}
    assert connection.calls[0][1][1] == '{"query":"private detail"}'
    assert "p.quote_text" in _PROVENANCE_QUOTE_SQL
    assert "o.summary" in _OBSERVATION_SUMMARY_SQL
    assert "a.metadata" in _AUDIT_METADATA_SQL
    assert "state" in _CHECKPOINT_STATE_SQL
    assert _PGCRYPTO_JSON_KEY in _AUDIT_METADATA_SQL
