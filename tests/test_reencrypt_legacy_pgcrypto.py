from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "reencrypt_legacy_pgcrypto.py"
    spec = importlib.util.spec_from_file_location("reencrypt_legacy_pgcrypto_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class _FakeConnection:
    def __init__(self, *, always_pending: bool = False) -> None:
        self.count_calls = 0
        self.always_pending = always_pending
        self.updates: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql: str, params=None):
        if sql.lstrip().startswith("select count(*)"):
            self.count_calls += 1
            # Each step reports one legacy row before its write and none after.
            total = 1 if self.always_pending or self.count_calls % 2 else 0
            return SimpleNamespace(fetchone=lambda: {"total": total})
        self.updates.append((sql, tuple(params or ())))
        return SimpleNamespace(rowcount=1)

    def commit(self) -> None:
        self.commits += 1


def test_reencrypt_legacy_covers_every_supported_plaintext_field(monkeypatch) -> None:
    script = _load_script()
    connection = _FakeConnection()
    monkeypatch.setattr(script.psycopg, "connect", lambda *_args, **_kwargs: connection)

    report = script.reencrypt_legacy(
        "postgresql://admin/memory",
        "test-key",
        batch_size=10,
    )

    assert report["ok"] is True
    assert [step["name"] for step in report["steps"]] == [
        "memory_items.text",
        "memory_provenance.quote_text",
        "conversation_messages.content",
        "memory_proposals.proposal",
        "memory_proposals.evidence",
        "observations.summary",
        "audit_events.metadata",
        "checkpoints.state",
    ]
    assert len(connection.updates) == len(report["steps"])
    assert connection.commits == len(report["steps"])
    assert any("jsonb_build_object" in sql for sql, _params in connection.updates)
    assert all("pgp_sym_encrypt" in sql for sql, _params in connection.updates)


def test_reencrypt_legacy_dry_run_does_not_write(monkeypatch) -> None:
    script = _load_script()
    connection = _FakeConnection(always_pending=True)
    monkeypatch.setattr(script.psycopg, "connect", lambda *_args, **_kwargs: connection)

    report = script.reencrypt_legacy(
        "postgresql://admin/memory",
        "test-key",
        batch_size=10,
        dry_run=True,
    )

    assert report["dry_run"] is True
    assert report["complete"] is False
    assert connection.updates == []
    assert connection.commits == 0
